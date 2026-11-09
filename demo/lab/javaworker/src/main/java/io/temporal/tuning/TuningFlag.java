package io.temporal.tuning;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import com.fasterxml.jackson.databind.DeserializationFeature;
import com.fasterxml.jackson.databind.ObjectMapper;
import io.temporal.worker.WorkerFactoryOptions;
import io.temporal.worker.WorkerOptions;
import io.temporal.worker.tuning.ResourceBasedControllerOptions;
import io.temporal.worker.tuning.ResourceBasedSlotOptions;
import io.temporal.worker.tuning.ResourceBasedTuner;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.Map;

/**
 * The Worker-tuning feature flag: one value that decides HOW a Java Worker is tuned, and the
 * numbers it is tuned with.
 *
 * <p>Three modes:
 *
 * <ul>
 *   <li>{@code SDK_DEFAULTS} sets nothing. On 1.2x that is 5 WFT pollers, 5 Activity pollers, 200
 *       + 200 slots, sticky cache 600 and 600 Workflow threads. The control group: flip to it to
 *       prove the tuned config is worth having.
 *   <li>{@code MANUAL} uses fixed pollers and fixed slots. This is the only way to set pollers
 *       before SDK 1.30.0, which introduced PollerBehaviorAutoscaling.
 *   <li>{@code RESOURCE_BASED} sizes slots from CPU and heap with ResourceBasedTuner (1.24+).
 *       Pollers stay MANUAL in this mode: the tuner sizes slots and never touches pollers.
 * </ul>
 *
 * <p>Java cannot change a running Worker's options, so applying a new flag means building a new
 * WorkerFactory: see {@link SwappableWorker}. Rejecting a flag keeps the previous config; it never
 * falls back to SDK defaults. A Worker that quietly reverts to 5 pollers and 200 slots after a typo
 * looks healthy and is not running the config anyone chose.
 */
@JsonIgnoreProperties(ignoreUnknown = false)
public final class TuningFlag {

  public enum Mode {
    SDK_DEFAULTS,
    MANUAL,
    RESOURCE_BASED
  }

  public Mode mode = Mode.MANUAL;

  // Pollers. Java SPLITS workflowTaskPollers between the sticky and normal queues
  // (StickyQueueBalancer). Unlike Go, it does not add the same number again for the sticky queue,
  // so 4 here means 4 concurrent WFT polls in total. 1 is silently raised to 2.
  public int workflowTaskPollers = 2;
  public int activityTaskPollers = 2;

  // Slots. In RESOURCE_BASED these are the tuner's MAXIMUM slots.
  public int workflowTaskSlots = 100;
  public int activitySlots = 100;

  // Factory-level. A new WorkerFactory starts with an EMPTY sticky cache, so every swap replays
  // the history of every Workflow in flight once.
  public int stickyCacheSize = 600;
  // Each cached Workflow PARKS a thread between tasks. If stickyCacheSize exceeds this, the SDK
  // evicts cached Workflows to free threads (temporal_sticky_cache_thread_forced_eviction). A large
  // cache does nothing when there are not enough threads for it.
  public int maxWorkflowThreads = 600;

  public Resource resource = new Resource();

  /** Free-form tag recorded in logs and metrics, e.g. a change ticket. */
  public String label = "";

  /**
   * Required to run RESOURCE_BASED on 1.29.0-1.32.x, where ResourceBasedSlotSupplier can stop
   * handing out slots permanently (sdk-java #2779, fixed in 1.33.0).
   */
  public boolean acceptKnownTunerHangBug = false;

  @JsonIgnoreProperties(ignoreUnknown = false)
  public static final class Resource {
    // ResourceBasedTuner measures osBean.getSystemCpuLoad(): CPU of the whole host or container,
    // NOT this process. A busy sidecar in the same pod throttles this Worker's slots.
    public double targetCpu = 0.8;
    // Measured as JVM heap used / -Xmx, garbage included, so it saw-tooths with GC. It does not
    // see thread stacks, metaspace or direct buffers, so the container can be OOM-killed while the
    // tuner reads "under target". Set -Xmx explicitly, with headroom under the container limit.
    public double targetMemory = 0.8;
    public int minWorkflowTaskSlots = 2;
    public int minActivitySlots = 1;
    public long rampThrottleMs = 50;
  }

  private static final ObjectMapper JSON =
      new ObjectMapper().configure(DeserializationFeature.FAIL_ON_UNKNOWN_PROPERTIES, true);

  /**
   * Strict: an unknown field is an ERROR, not ignored. "activityPollers" for "activityTaskPollers"
   * otherwise parses cleanly and leaves the field at its default, the classic silent no-op.
   */
  public static TuningFlag parse(String json) throws Exception {
    TuningFlag f = JSON.readValue(json, TuningFlag.class);
    if (f.resource == null) f.resource = new Resource();
    return f;
  }

  /** Env-var form, for the first boot before any flag source answers. */
  public static TuningFlag fromEnv(Map<String, String> env) {
    TuningFlag f = new TuningFlag();
    f.mode = Mode.valueOf(env.getOrDefault("TUNING_MODE", f.mode.name()).toUpperCase(Locale.ROOT));
    f.workflowTaskPollers = intEnv(env, "TUNING_WFT_POLLERS", f.workflowTaskPollers);
    f.activityTaskPollers = intEnv(env, "TUNING_ACT_POLLERS", f.activityTaskPollers);
    f.workflowTaskSlots = intEnv(env, "TUNING_WFT_SLOTS", f.workflowTaskSlots);
    f.activitySlots = intEnv(env, "TUNING_ACT_SLOTS", f.activitySlots);
    f.stickyCacheSize = intEnv(env, "TUNING_STICKY_CACHE", f.stickyCacheSize);
    f.maxWorkflowThreads = intEnv(env, "TUNING_WORKFLOW_THREADS", f.maxWorkflowThreads);
    f.label = env.getOrDefault("TUNING_LABEL", "env");
    return f;
  }

  private static int intEnv(Map<String, String> env, String k, int def) {
    String v = env.get(k);
    return v == null || v.isBlank() ? def : Integer.parseInt(v.trim());
  }

  /** Result of validation: errors reject the flag, warnings are applied and reported. */
  public static final class Check {
    public final List<String> errors = new ArrayList<>();
    public final List<String> warnings = new ArrayList<>();

    public boolean ok() {
      return errors.isEmpty();
    }
  }

  public Check validate(String sdkVersion) {
    Check c = new Check();
    if (mode == null) {
      c.errors.add("mode is required: SDK_DEFAULTS, MANUAL or RESOURCE_BASED");
      return c;
    }
    if (mode == Mode.SDK_DEFAULTS) return c;

    // 0 does NOT mean "none" or "unlimited" in WorkerOptions. It means "use the default" (5
    // pollers, 200 slots), so a 0 meant to switch something off silently gives you the default.
    for (Object[] p :
        new Object[][] {
          {"workflowTaskPollers", workflowTaskPollers},
          {"activityTaskPollers", activityTaskPollers},
          {"workflowTaskSlots", workflowTaskSlots},
          {"activitySlots", activitySlots},
          {"stickyCacheSize", stickyCacheSize},
          {"maxWorkflowThreads", maxWorkflowThreads},
        }) {
      if ((int) p[1] <= 0) {
        c.errors.add(
            p[0] + "=" + p[1] + ": the SDK treats 0 as 'use the default', not 'none'. Set a"
                + " positive value, or use mode SDK_DEFAULTS.");
      }
    }
    if (!c.ok()) return c;

    if (workflowTaskPollers == 1) {
      c.warnings.add(
          "workflowTaskPollers=1 is raised to 2 by the SDK: Java alternates its WFT pollers"
              + " between the sticky and normal queues.");
    }
    if (activityTaskPollers > activitySlots && mode == Mode.MANUAL) {
      c.warnings.add(
          "activityTaskPollers ("
              + activityTaskPollers
              + ") > activitySlots ("
              + activitySlots
              + "): a poller reserves a slot before it polls, so the extra pollers never poll.");
    }
    if (stickyCacheSize > maxWorkflowThreads) {
      c.warnings.add(
          "stickyCacheSize ("
              + stickyCacheSize
              + ") > maxWorkflowThreads ("
              + maxWorkflowThreads
              + "): each cached Workflow parks a thread, so the cache can never fill. Expect"
              + " temporal_sticky_cache_thread_forced_eviction and replays.");
    }
    if (activitySlots > 2000) {
      c.warnings.add(
          "activitySlots="
              + activitySlots
              + ": each busy Activity slot is a platform thread (about 1 MB of stack, outside the"
              + " heap). Check container memory, not just -Xmx.");
    }

    if (mode == Mode.RESOURCE_BASED) {
      if (!(resource.targetCpu > 0 && resource.targetCpu <= 1)) {
        c.errors.add("resource.targetCpu must be in (0, 1], got " + resource.targetCpu);
      }
      if (!(resource.targetMemory > 0 && resource.targetMemory <= 1)) {
        c.errors.add("resource.targetMemory must be in (0, 1], got " + resource.targetMemory);
      }
      if (resource.minActivitySlots > activitySlots
          || resource.minWorkflowTaskSlots > workflowTaskSlots) {
        c.errors.add("resource minimum slots exceed the maximum (activitySlots/workflowTaskSlots)");
      }
      if (hasTunerHangBug(sdkVersion) && !acceptKnownTunerHangBug) {
        c.errors.add(
            "RESOURCE_BASED refused on SDK "
                + sdkVersion
                + ": 1.29.0-1.32.x ResourceBasedSlotSupplier can stop issuing slots permanently"
                + " after sustained starvation (sdk-java #2779, fixed in 1.33.0). Upgrade, use"
                + " 1.28.x, or set acceptKnownTunerHangBug=true.");
      }
      c.warnings.add(
          "RESOURCE_BASED: CPU is the whole host/container (getSystemCpuLoad), not this process;"
              + " memory is JVM heap / -Xmx. Pollers stay fixed at the values given.");
    }
    return c;
  }

  /** 1.29.0 <= v < 1.33.0. */
  static boolean hasTunerHangBug(String v) {
    int[] p = semver(v);
    return p != null && p[0] == 1 && p[1] >= 29 && p[1] < 33;
  }

  static int[] semver(String v) {
    if (v == null) return null;
    String[] s = v.replaceFirst("^v", "").split("[.-]");
    try {
      return new int[] {Integer.parseInt(s[0]), Integer.parseInt(s[1])};
    } catch (RuntimeException e) {
      return null;
    }
  }

  public WorkerOptions workerOptions() {
    WorkerOptions.Builder b = WorkerOptions.newBuilder();
    switch (mode) {
      case SDK_DEFAULTS:
        break;
      case MANUAL:
        b.setMaxConcurrentWorkflowTaskPollers(workflowTaskPollers)
            .setMaxConcurrentActivityTaskPollers(activityTaskPollers)
            .setMaxConcurrentWorkflowTaskExecutionSize(workflowTaskSlots)
            .setMaxConcurrentActivityExecutionSize(activitySlots);
        break;
      case RESOURCE_BASED:
        // WorkerOptions REJECTS a tuner combined with any setMaxConcurrent*ExecutionSize
        // ("must not be set if workerTuner is set"), so slot limits go into the tuner's options.
        b.setMaxConcurrentWorkflowTaskPollers(workflowTaskPollers)
            .setMaxConcurrentActivityTaskPollers(activityTaskPollers)
            .setWorkerTuner(
                ResourceBasedTuner.newBuilder()
                    .setControllerOptions(
                        ResourceBasedControllerOptions.newBuilder(
                                resource.targetMemory, resource.targetCpu)
                            .build())
                    .setWorkflowSlotOptions(
                        ResourceBasedSlotOptions.newBuilder()
                            .setMinimumSlots(resource.minWorkflowTaskSlots)
                            .setMaximumSlots(workflowTaskSlots)
                            .setRampThrottle(Duration.ZERO)
                            .build())
                    .setActivitySlotOptions(
                        ResourceBasedSlotOptions.newBuilder()
                            .setMinimumSlots(resource.minActivitySlots)
                            .setMaximumSlots(activitySlots)
                            .setRampThrottle(Duration.ofMillis(resource.rampThrottleMs))
                            .build())
                    .build());
        break;
    }
    return b.build();
  }

  public WorkerFactoryOptions factoryOptions() {
    WorkerFactoryOptions.Builder b = WorkerFactoryOptions.newBuilder();
    if (mode != Mode.SDK_DEFAULTS) {
      b.setWorkflowCacheSize(stickyCacheSize).setMaxWorkflowThreadCount(maxWorkflowThreads);
    }
    return b.build();
  }

  /** What the SDK will ACTUALLY run with, after its own adjustments, for metrics and logs. */
  public Map<String, Integer> effective() {
    if (mode == Mode.SDK_DEFAULTS) {
      return Map.of(
          "workflow_task_pollers", 5,
          "activity_task_pollers", 5,
          "workflow_task_slots", 200,
          "activity_slots", 200,
          "sticky_cache_size", 600,
          "max_workflow_threads", 600);
    }
    return Map.of(
        "workflow_task_pollers", Math.max(2, workflowTaskPollers),
        "activity_task_pollers", activityTaskPollers,
        "workflow_task_slots", workflowTaskSlots,
        "activity_slots", activitySlots,
        "sticky_cache_size", stickyCacheSize,
        "max_workflow_threads", maxWorkflowThreads);
  }

  /** Two flags that would build the same Worker. Label and acknowledgements don't count. */
  public boolean sameWorkerAs(TuningFlag o) {
    return o != null
        && mode == o.mode
        && workflowTaskPollers == o.workflowTaskPollers
        && activityTaskPollers == o.activityTaskPollers
        && workflowTaskSlots == o.workflowTaskSlots
        && activitySlots == o.activitySlots
        && stickyCacheSize == o.stickyCacheSize
        && maxWorkflowThreads == o.maxWorkflowThreads
        && resource.targetCpu == o.resource.targetCpu
        && resource.targetMemory == o.resource.targetMemory
        && resource.minActivitySlots == o.resource.minActivitySlots
        && resource.minWorkflowTaskSlots == o.resource.minWorkflowTaskSlots
        && resource.rampThrottleMs == o.resource.rampThrottleMs;
  }

  @Override
  public String toString() {
    return String.format(
        "%s wftPollers=%d actPollers=%d wftSlots=%d actSlots=%d cache=%d threads=%d%s label=%s",
        mode,
        workflowTaskPollers,
        activityTaskPollers,
        workflowTaskSlots,
        activitySlots,
        stickyCacheSize,
        maxWorkflowThreads,
        mode == Mode.RESOURCE_BASED
            ? String.format(" cpu=%.2f mem=%.2f", resource.targetCpu, resource.targetMemory)
            : "",
        label);
  }
}
