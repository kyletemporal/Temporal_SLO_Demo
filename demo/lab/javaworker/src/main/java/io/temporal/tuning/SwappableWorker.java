package io.temporal.tuning;

import io.micrometer.core.instrument.Counter;
import io.micrometer.core.instrument.Gauge;
import io.micrometer.core.instrument.MeterRegistry;
import io.micrometer.core.instrument.Tags;
import io.temporal.client.WorkflowClient;
import io.temporal.worker.Worker;
import io.temporal.worker.WorkerFactory;
import java.time.Duration;
import java.time.Instant;
import java.util.EnumMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicLong;
import java.util.concurrent.atomic.AtomicReference;
import java.util.function.Consumer;
import java.util.function.Function;
import java.util.logging.Logger;

/**
 * A Worker whose tuning comes from a {@link TuningFlag} and can change at runtime.
 *
 * <p>A Java Worker's options are fixed once its WorkerFactory starts. {@code newWorker} throws
 * after {@code start()}, and no setter exists for pollers or slots. So a flag change is a SWAP:
 *
 * <ol>
 *   <li>build and START a new WorkerFactory on the same Task Queue, so the queue always has
 *       pollers;
 *   <li>then shut the old one down gracefully. In-flight Activities finish, and in-flight Workflow
 *       Tasks complete.
 * </ol>
 *
 * <p>What a swap costs, and what this class does about it:
 *
 * <ul>
 *   <li><b>Cold sticky cache.</b> The new factory starts with an empty cache, so every open
 *       Workflow replays once on its next task. Swaps are therefore rate-limited ({@code
 *       minSwapInterval}). Flags that flap (a percentage rollout re-evaluating every few seconds)
 *       would otherwise thrash the cache. Changes inside the window are coalesced, and the latest
 *       one wins.
 *   <li><b>How much it costs, measured</b> (lab, 1.28.4, ~40 Workflows open at 4/s): the hit
 *       ratio dipped to 75% for one 10 s window and was back at 100% within about 6 s. The worst
 *       Workflow Task schedule-to-start across the swap was 101 ms, so we did NOT see the 5 s
 *       sticky-timeout stall you might expect. Replay was cheap here because the histories were
 *       short. With long histories the dip costs more, so measure it on your own Workflows.
 *   <li><b>Two slot pools during the drain.</b> While the old factory finishes in-flight work,
 *       the process holds both generations' threads. Size the container for that, or the swap
 *       itself causes the OOM.
 * </ul>
 *
 * <p>A flag that fails validation, or a swap that fails to start, KEEPS the running Worker. It
 * never falls back to SDK defaults.
 */
public final class SwappableWorker implements AutoCloseable {
  private static final Logger log = Logger.getLogger(SwappableWorker.class.getName());

  private final String taskQueue;
  private final String sdkVersion;
  private final Function<String, WorkflowClient> clientFor; // instance id -> client
  private final Consumer<Worker> register;
  private final Consumer<Running> onRetire;
  private final Duration minSwapInterval;
  private final Duration drainTimeout;
  private final ScheduledExecutorService exec =
      Executors.newSingleThreadScheduledExecutor(
          r -> {
            Thread t = new Thread(r, "tuning-swap");
            t.setDaemon(true);
            return t;
          });

  private final AtomicReference<Running> current = new AtomicReference<>();
  private final AtomicReference<TuningFlag> pending = new AtomicReference<>();
  // The last flag ACCEPTED, not the last one running. Comparing against current() alone let a
  // second submit of the same value, arriving before the first swap finished, start a second
  // identical generation. Measured at boot in file mode: g1 and g2 with the same config, a
  // wasted swap and a cold cache.
  private volatile TuningFlag accepted;
  private volatile Instant lastSwap = Instant.EPOCH;
  private volatile boolean scheduled;
  private final AtomicLong swapSeq = new AtomicLong();

  // Metrics. The point of exporting the FLAG as a metric: a dashboard can then show which config
  // was live when a latency changed. Without that, "did the flip help?" is answered from memory.
  private final Counter swaps;
  private final Counter rejected;
  private final Map<TuningFlag.Mode, AtomicLong> modeGauge = new EnumMap<>(TuningFlag.Mode.class);
  private final Map<String, AtomicLong> settingGauge = new ConcurrentHashMap<>();
  private final AtomicLong warningsGauge = new AtomicLong();
  private final AtomicLong lastSwapGauge = new AtomicLong();
  private final MeterRegistry registry;
  private final Tags tags;

  /** One running Worker generation. */
  public static final class Running {
    public final String instance;
    public final TuningFlag flag;
    public final WorkerFactory factory;
    public final WorkflowClient client;

    Running(String instance, TuningFlag flag, WorkerFactory factory, WorkflowClient client) {
      this.instance = instance;
      this.flag = flag;
      this.factory = factory;
      this.client = client;
    }
  }

  public SwappableWorker(
      String taskQueue,
      String namespace,
      String sdkVersion,
      MeterRegistry registry,
      Function<String, WorkflowClient> clientFor,
      Consumer<Worker> register,
      Consumer<Running> onRetire,
      Duration minSwapInterval,
      Duration drainTimeout) {
    this.taskQueue = taskQueue;
    this.sdkVersion = sdkVersion;
    this.clientFor = clientFor;
    this.register = register;
    this.onRetire = onRetire;
    this.minSwapInterval = minSwapInterval;
    this.drainTimeout = drainTimeout;
    this.registry = registry;
    this.tags = Tags.of("task_queue", taskQueue, "namespace", namespace, "sdk_version", sdkVersion);
    this.swaps =
        Counter.builder("worker_tuning_swaps")
            .description("Worker generations started because the tuning flag changed")
            .tags(tags)
            .register(registry);
    this.rejected =
        Counter.builder("worker_tuning_flag_rejected")
            .description("Flag values refused by validation; the previous config kept running")
            .tags(tags)
            .register(registry);
    for (TuningFlag.Mode m : TuningFlag.Mode.values()) {
      AtomicLong g = new AtomicLong();
      modeGauge.put(m, g);
      // All modes always present (0/1), so a "which mode" panel never shows No data.
      Gauge.builder("worker_tuning_mode", g, AtomicLong::get)
          .tags(tags.and("mode", m.name()))
          .register(registry);
    }
    Gauge.builder("worker_tuning_flag_warnings", warningsGauge, AtomicLong::get)
        .description("Landmine warnings on the flag currently applied")
        .tags(tags)
        .register(registry);
    Gauge.builder("worker_tuning_last_swap_timestamp_seconds", lastSwapGauge, AtomicLong::get)
        .tags(tags)
        .register(registry);
  }

  /** Submit a flag value. Returns the validation result; application may be deferred. */
  public synchronized TuningFlag.Check submit(TuningFlag flag) {
    TuningFlag.Check check = flag.validate(sdkVersion);
    if (!check.ok()) {
      rejected.increment();
      log.warning("tuning flag REJECTED, keeping current config: " + check.errors);
      return check;
    }
    Running cur = current.get();
    if (accepted != null && accepted.sameWorkerAs(flag)) {
      return check; // e.g. only the label changed: no swap, no cold cache
    }
    accepted = flag;
    for (String w : check.warnings) log.warning("tuning flag: " + w);
    pending.set(flag);
    Duration since = Duration.between(lastSwap, Instant.now());
    if (cur == null || since.compareTo(minSwapInterval) >= 0) {
      exec.execute(this::applyPending);
    } else if (!scheduled) {
      scheduled = true;
      long wait = minSwapInterval.minus(since).toMillis();
      log.info("tuning flag change coalesced; applying in " + wait + "ms (minSwapInterval)");
      exec.schedule(this::applyPending, wait, TimeUnit.MILLISECONDS);
    }
    return check;
  }

  private void applyPending() {
    scheduled = false;
    TuningFlag flag = pending.getAndSet(null);
    if (flag == null) return;
    try {
      swapTo(flag);
    } catch (RuntimeException e) {
      // e.g. WorkerOptions' own checkState. The old Worker is still running.
      rejected.increment();
      log.severe("swap FAILED, keeping current Worker: " + e);
      // Let the same value be retried: the running Worker is still the old one.
      Running cur = current.get();
      accepted = cur == null ? null : cur.flag;
    }
  }

  private void swapTo(TuningFlag flag) {
    String instance = taskQueue + "-g" + swapSeq.incrementAndGet();
    WorkflowClient client = clientFor.apply(instance);
    WorkerFactory factory;
    try {
      factory = WorkerFactory.newInstance(client, flag.factoryOptions());
      Worker w = factory.newWorker(taskQueue, flag.workerOptions());
      register.accept(w);
      factory.start(); // the new generation polls BEFORE the old one stops
    } catch (RuntimeException e) {
      client.getWorkflowServiceStubs().shutdownNow();
      throw e;
    }
    Running next = new Running(instance, flag, factory, client);
    Running old = current.getAndSet(next);
    lastSwap = Instant.now();
    lastSwapGauge.set(lastSwap.getEpochSecond());
    swaps.increment();
    publish(flag);
    log.info("tuning applied (" + instance + "): " + flag);
    if (old != null) {
      Thread t =
          new Thread(
              () -> {
                old.factory.shutdown();
                old.factory.awaitTermination(drainTimeout.toMillis(), TimeUnit.MILLISECONDS);
                if (!old.factory.isTerminated()) {
                  log.warning(old.instance + " did not drain in " + drainTimeout + "; forcing");
                  old.factory.shutdownNow();
                }
                old.client.getWorkflowServiceStubs().shutdown();
                onRetire.accept(old);
                log.info("drained " + old.instance);
              },
              "tuning-drain-" + old.instance);
      t.setDaemon(true);
      t.start();
    }
  }

  private void publish(TuningFlag flag) {
    for (Map.Entry<TuningFlag.Mode, AtomicLong> e : modeGauge.entrySet()) {
      e.getValue().set(e.getKey() == flag.mode ? 1 : 0);
    }
    for (Map.Entry<String, Integer> e : flag.effective().entrySet()) {
      settingGauge
          .computeIfAbsent(
              e.getKey(),
              k -> {
                AtomicLong g = new AtomicLong();
                Gauge.builder("worker_tuning_setting", g, AtomicLong::get)
                    .description(
                        "Effective tuning value from the flag. Java <1.30 emits no num_pollers,"
                            + " so this is the only poller count on a 1.2x dashboard.")
                    .tags(tags.and("setting", k))
                    .register(registry);
                return g;
              })
          .set(e.getValue());
    }
    warningsGauge.set(flag.validate(sdkVersion).warnings.size());
  }

  public Running current() {
    return current.get();
  }

  public List<String> warnings() {
    Running r = current.get();
    return r == null ? List.of() : r.flag.validate(sdkVersion).warnings;
  }

  @Override
  public void close() {
    Running r = current.getAndSet(null);
    if (r != null) {
      r.factory.shutdown();
      r.factory.awaitTermination(drainTimeout.toMillis(), TimeUnit.MILLISECONDS);
      r.client.getWorkflowServiceStubs().shutdown();
    }
    exec.shutdownNow();
  }
}
