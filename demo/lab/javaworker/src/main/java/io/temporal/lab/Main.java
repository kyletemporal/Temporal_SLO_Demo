package io.temporal.lab;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpServer;
import com.uber.m3.tally.RootScopeBuilder;
import com.uber.m3.tally.Scope;
import io.micrometer.core.instrument.Meter;
import io.micrometer.core.instrument.binder.jvm.JvmGcMetrics;
import io.micrometer.core.instrument.binder.jvm.JvmMemoryMetrics;
import io.micrometer.core.instrument.binder.jvm.JvmThreadMetrics;
import io.micrometer.core.instrument.binder.system.ProcessorMetrics;
import io.micrometer.core.instrument.config.MeterFilter;
import io.micrometer.core.instrument.distribution.DistributionStatisticConfig;
import io.micrometer.prometheus.PrometheusConfig;
import io.micrometer.prometheus.PrometheusMeterRegistry;
import io.temporal.activity.Activity;
import io.temporal.client.WorkflowClient;
import io.temporal.client.WorkflowClientOptions;
import io.temporal.common.reporter.MicrometerClientStatsReporter;
import io.temporal.serviceclient.SimpleSslContextBuilder;
import io.temporal.serviceclient.Version;
import io.temporal.serviceclient.WorkflowServiceStubs;
import io.temporal.serviceclient.WorkflowServiceStubsOptions;
import io.temporal.tuning.FileFlagSource;
import io.temporal.tuning.GenerationalGaugeReporter;
import io.temporal.tuning.SwappableWorker;
import io.temporal.tuning.TuningFlag;
import java.io.FileInputStream;
import java.io.OutputStream;
import java.lang.management.ManagementFactory;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ThreadLocalRandom;
import java.util.concurrent.atomic.AtomicReference;
import java.util.logging.Logger;

/**
 * Java Worker for the tuning lab, and a standalone reference Worker for the k6 test.
 *
 * <pre>
 *   FLAG_SOURCE=controller  (lab)    the lab UI is the flag service; config polled every 1 s
 *   FLAG_SOURCE=file        (anyone) a JSON TuningFlag file, e.g. a mounted ConfigMap
 * </pre>
 *
 * Connection: TEMPORAL_ADDRESS, TEMPORAL_NAMESPACE, and either TEMPORAL_API_KEY (TLS + API key)
 * or TEMPORAL_TLS_CERT + TEMPORAL_TLS_KEY (mTLS), or neither (plaintext, self-hosted/dev).
 */
public final class Main {
  private static final Logger log = Logger.getLogger(Main.class.getName());
  private static final ObjectMapper JSON = new ObjectMapper();
  private static final Map<String, String> ENV = System.getenv();

  static String env(String k, String def) {
    String v = ENV.get(k);
    return v == null || v.isBlank() ? def : v;
  }

  /** The Activity's behaviour. Live: read on every Activity, like the Go and Python lanes. */
  record Workload(String profile, int durationMs, int jitterPct) {}

  static final AtomicReference<Workload> workload =
      new AtomicReference<>(
          new Workload(
              env("ACTIVITY_PROFILE", "sleep"),
              Integer.parseInt(env("ACTIVITY_MS", "150")),
              Integer.parseInt(env("ACTIVITY_JITTER_PCT", "20"))));

  static final String LANE = env("LANE", "java");
  static final String CONTROLLER = env("CONTROLLER_URL", "http://lab-controller:8095");
  static final HttpClient http =
      HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(3)).build();
  static double roundsPerMs;

  public static void main(String[] args) throws Exception {
    String sdk = Version.LIBRARY_VERSION;
    String ns = env("TEMPORAL_NAMESPACE", "lab");
    String taskQueue = env("TASK_QUEUE", "lab-" + LANE);
    String source = env("FLAG_SOURCE", "controller");
    log.info("Java SDK " + sdk + ", namespace " + ns + ", task queue " + taskQueue
        + ", flag source " + source);

    PrometheusMeterRegistry registry = new PrometheusMeterRegistry(PrometheusConfig.DEFAULT);
    // Micrometer's default histogram for a Timer tops out at 30 s. Every schedule-to-start above
    // that lands in +Inf, and histogram_quantile then reports the top finite bucket: a 2-minute
    // backlog reads "30 s" on the dashboard. Raise the ceiling BEFORE any meter registers.
    registry
        .config()
        .meterFilter(
            new MeterFilter() {
              @Override
              public DistributionStatisticConfig configure(
                  Meter.Id id, DistributionStatisticConfig config) {
                if (id.getType() == Meter.Type.TIMER && id.getName().startsWith("temporal_")) {
                  return DistributionStatisticConfig.builder()
                      .maximumExpectedValue((double) Duration.ofMinutes(10).toNanos())
                      .build()
                      .merge(config);
                }
                return config;
              }
            });
    new JvmMemoryMetrics().bindTo(registry);
    new JvmThreadMetrics().bindTo(registry);
    new JvmGcMetrics().bindTo(registry);
    new ProcessorMetrics().bindTo(registry);
    serveMetrics(registry, Integer.parseInt(env("METRICS_PORT", "9464")));

    Telemetry telemetry = new Telemetry();
    // NOT a bare MicrometerClientStatsReporter: across a swap its gauges freeze at the old
    // generation's value (see GenerationalGaugeReporter for the measured case).
    GenerationalGaugeReporter gauges =
        new GenerationalGaugeReporter(registry, new MicrometerClientStatsReporter(registry));
    Map<String, Scope> scopes = new ConcurrentHashMap<>();
    roundsPerMs = calibrate();
    log.info(String.format("calibrated CPU work: %.0f sha256 rounds per ms", roundsPerMs));

    SwappableWorker worker =
        new SwappableWorker(
            taskQueue,
            ns,
            sdk,
            registry,
            instance -> {
              // One client (and tally scope) per generation, so the two generations' gauges stay
              // separate in the live view during a swap. Prometheus sees one series either way.
              Scope scope =
                  new RootScopeBuilder()
                      .reporter(telemetry.tee(gauges.forGeneration(instance), instance))
                      .reportEvery(com.uber.m3.util.Duration.ofSeconds(1));
              scopes.put(instance, scope);
              return WorkflowClient.newInstance(
                  stubs(scope), WorkflowClientOptions.newBuilder().setNamespace(ns).build());
            },
            w -> {
              w.registerWorkflowImplementationTypes(Lab.LabWorkflowImpl.class);
              w.registerActivitiesImplementations((Lab.LabActivity) Main::activity);
            },
            retired -> {
              Scope s = scopes.remove(retired.instance);
              if (s != null) {
                try {
                  s.close();
                } catch (Exception e) {
                  log.warning("closing metrics scope " + retired.instance + ": " + e);
                }
              }
              telemetry.dropInstance(retired.instance);
              gauges.retire(retired.instance);
            },
            Duration.ofSeconds(Long.parseLong(env("TUNING_MIN_SWAP_INTERVAL_S", "10"))),
            Duration.ofSeconds(30));
    Runtime.getRuntime().addShutdownHook(new Thread(worker::close));

    if (source.equals("file")) {
      Path path = Path.of(env("FLAG_FILE", "/config/tuning.json"));
      // Boot from the file if it is valid, so a pod does not start on env values and then swap
      // seconds later. Env is the fallback, and both paths are logged.
      TuningFlag boot = TuningFlag.fromEnv(ENV);
      try {
        boot = TuningFlag.parse(Files.readString(path));
        log.info("boot flag from " + path);
      } catch (Exception e) {
        log.warning("boot flag from env (" + path + " unusable: " + e.getMessage() + ")");
      }
      // A rejected BOOT flag must exit. "Keep the current config" means keep NOTHING at boot: the
      // process stayed up, looked healthy, and had zero pollers on the queue (measured: 1.29.0 with
      // RESOURCE_BASED). Exiting turns that into a visible crash loop.
      TuningFlag.Check bootCheck = worker.submit(boot);
      if (!bootCheck.ok()) {
        log.severe("boot tuning flag rejected, exiting: " + bootCheck.errors);
        System.exit(2);
      }
      new FileFlagSource(path, worker::submit).start(2000);
      Thread.currentThread().join();
    } else {
      runLab(worker, telemetry, sdk);
    }
  }

  static WorkflowServiceStubs stubs(Scope scope) {
    WorkflowServiceStubsOptions.Builder b =
        WorkflowServiceStubsOptions.newBuilder()
            .setTarget(env("TEMPORAL_ADDRESS", "temporal:7233"))
            .setMetricsScope(scope);
    String apiKey = env("TEMPORAL_API_KEY", "");
    String cert = env("TEMPORAL_TLS_CERT", "");
    try {
      if (!apiKey.isEmpty()) {
        b.setEnableHttps(true).addApiKey(() -> apiKey);
        // Before 1.28.1 the SDK does NOT send the temporal-namespace header, and Cloud rejects an
        // API-key call without it as PERMISSION_DENIED "Request unauthorized", with a valid key.
        // 1.28.1+ sets it itself (release notes), so add it only on older SDKs.
        if (olderThan(Version.LIBRARY_VERSION, 1, 28, 1)) {
          io.grpc.Metadata md = new io.grpc.Metadata();
          md.put(
              io.grpc.Metadata.Key.of("temporal-namespace", io.grpc.Metadata.ASCII_STRING_MARSHALLER),
              env("TEMPORAL_NAMESPACE", "lab"));
          b.addGrpcMetadataProvider(() -> md);
        }
      } else if (!cert.isEmpty()) {
        try (FileInputStream c = new FileInputStream(cert);
            FileInputStream k = new FileInputStream(env("TEMPORAL_TLS_KEY", ""))) {
          b.setSslContext(SimpleSslContextBuilder.forPKCS8(c, k).build());
        }
      }
    } catch (Exception e) {
      throw new IllegalStateException("TLS setup failed: " + e, e);
    }
    return WorkflowServiceStubs.newServiceStubs(b.build());
  }

  static boolean olderThan(String v, int maj, int min, int patch) {
    String[] p = v.replaceFirst("^v", "").split("[.-]");
    int[] a = new int[3];
    for (int i = 0; i < 3 && i < p.length; i++) {
      try {
        a[i] = Integer.parseInt(p[i]);
      } catch (NumberFormatException e) {
        return false;
      }
    }
    return a[0] != maj ? a[0] < maj : a[1] != min ? a[1] < min : a[2] < patch;
  }

  // ---------------------------------------------------------------------------------------------
  // Activity
  // ---------------------------------------------------------------------------------------------

  static void activity(Lab.ActivityInput in) {
    Workload w = workload.get();
    double d = w.durationMs();
    if (w.jitterPct() > 0) {
      d += d * w.jitterPct() / 100.0 * (ThreadLocalRandom.current().nextDouble() * 2 - 1);
    }
    long ms = Math.max(0, Math.round(d));
    try {
      switch (w.profile()) {
        case "cpu" -> burn((long) (roundsPerMs * ms));
        case "io" -> {
          HttpResponse<Void> r =
              http.send(
                  HttpRequest.newBuilder(
                          URI.create(CONTROLLER + "/downstream?lane=" + LANE + "&ms=" + ms))
                      .timeout(Duration.ofMinutes(2))
                      .build(),
                  HttpResponse.BodyHandlers.discarding());
          if (r.statusCode() != 200) throw new IllegalStateException("downstream " + r.statusCode());
        }
        default -> Thread.sleep(ms);
      }
    } catch (InterruptedException e) {
      Thread.currentThread().interrupt();
      throw Activity.wrap(e);
    } catch (Exception e) {
      throw Activity.wrap(e);
    }
  }

  static void burn(long rounds) {
    try {
      MessageDigest md = MessageDigest.getInstance("SHA-256");
      byte[] b = "temporal-tuning-lab".getBytes(StandardCharsets.UTF_8);
      for (long i = 0; i < rounds; i++) b = md.digest(b);
    } catch (Exception e) {
      throw new IllegalStateException(e);
    }
  }

  static double calibrate() {
    burn(200_000); // JIT warm-up: an unwarmed calibration overstates the cost 3-5x
    long rounds = 200_000, t0 = System.nanoTime();
    burn(rounds);
    return rounds / Math.max((System.nanoTime() - t0) / 1e6, 1e-3);
  }

  // ---------------------------------------------------------------------------------------------
  // Lab: the controller is the flag service, and the Worker reports what it measures.
  // ---------------------------------------------------------------------------------------------

  static TuningFlag flagFromLane(JsonNode c) {
    TuningFlag f = new TuningFlag();
    f.mode =
        switch (c.path("tuningMode").asText("manual")) {
          case "resource" -> TuningFlag.Mode.RESOURCE_BASED;
          case "sdk-defaults" -> TuningFlag.Mode.SDK_DEFAULTS;
          default -> TuningFlag.Mode.MANUAL;
        };
    f.workflowTaskPollers = c.path("wfPollers").asInt();
    f.activityTaskPollers = c.path("actPollers").asInt();
    f.workflowTaskSlots = c.path("wfSlots").asInt();
    f.activitySlots = c.path("actSlots").asInt();
    f.stickyCacheSize = c.path("cacheSize").asInt();
    f.maxWorkflowThreads = c.path("workflowThreads").asInt(600);
    f.resource.targetCpu = c.path("targetCpuPct").asInt(80) / 100.0;
    f.label = "lab";
    return f;
  }

  static void runLab(SwappableWorker worker, Telemetry tel, String sdk) throws Exception {
    String workerId = LANE + "-" + InetAddress.getLocalHost().getHostName();
    String startedAt = Instant.now().toString();
    long seenVersion = -1, appliedVersion = 0;
    TuningFlag desired = null;
    List<String> rejected = List.of();
    var os = (com.sun.management.OperatingSystemMXBean) ManagementFactory.getOperatingSystemMXBean();
    long lastCpu = os.getProcessCpuTime(), lastAt = System.nanoTime(), lastPoll = 0;
    boolean loggedNames = false;

    while (true) {
      long now = System.nanoTime();
      if (now - lastPoll >= 1_000_000_000L) {
        lastPoll = now;
        try {
          JsonNode msg = JSON.readTree(get(CONTROLLER + "/api/config?lane=" + LANE));
          long v = msg.path("version").asLong();
          if (v != seenVersion) {
            JsonNode c = msg.path("config");
            workload.set(
                new Workload(
                    c.path("profile").asText("sleep"),
                    c.path("durationMs").asInt(150),
                    c.path("jitterPct").asInt(0)));
            desired = flagFromLane(c);
            TuningFlag.Check chk = worker.submit(desired);
            rejected = chk.errors;
            seenVersion = v;
          }
        } catch (Exception e) {
          log.warning("config poll failed (keeping current Worker): " + e.getMessage());
        }
      }
      SwappableWorker.Running cur = worker.current();
      if (cur != null && desired != null && cur.flag.sameWorkerAs(desired)) {
        appliedVersion = seenVersion;
      }

      Thread.sleep(500);
      long cpu = os.getProcessCpuTime(), at = System.nanoTime();
      double cores = (cpu - lastCpu) / (double) (at - lastAt);
      lastCpu = cpu;
      lastAt = at;
      if (!loggedNames && !tel.seenNames.isEmpty() && tel.counters.containsKey("wft_completed")) {
        log.info("SDK metric names seen: " + new java.util.TreeSet<>(tel.seenNames));
        loggedNames = true;
      }

      List<String> notes = new ArrayList<>();
      notes.add("Java " + sdk + (cur == null ? "" : " · " + cur.flag.mode));
      if (!rejected.isEmpty()) notes.add("FLAG REJECTED: " + String.join("; ", rejected));
      notes.addAll(worker.warnings());
      if (!tel.seenNames.contains("num_pollers")) {
        notes.add("no num_pollers metric before SDK 1.30: poller dots show the configured count");
      }

      Map<String, Object> rep = new HashMap<>();
      rep.put("lane", LANE);
      rep.put("workerId", workerId);
      rep.put("sdk", "java " + sdk);
      rep.put("configVersion", appliedVersion);
      rep.put("startedAt", startedAt);
      rep.put("counters", tel.counters);
      rep.put("gauges", tel.summedGauges());
      rep.put("latency", tel.latency());
      rep.put("cpuCores", cores);
      rep.put("cpuLimit", Runtime.getRuntime().availableProcessors());
      rep.put("rssMB", rssMb());
      rep.put("note", String.join(" · ", notes));
      try {
        post(CONTROLLER + "/api/report", JSON.writeValueAsString(rep));
      } catch (Exception ignored) {
        // controller restarting; the next report catches up
      }
    }
  }

  static String get(String url) throws Exception {
    return http.send(
            HttpRequest.newBuilder(URI.create(url)).timeout(Duration.ofSeconds(5)).build(),
            HttpResponse.BodyHandlers.ofString())
        .body();
  }

  static void post(String url, String body) throws Exception {
    http.send(
        HttpRequest.newBuilder(URI.create(url))
            .timeout(Duration.ofSeconds(5))
            .header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(body))
            .build(),
        HttpResponse.BodyHandlers.discarding());
  }

  static double rssMb() {
    try {
      for (String l : Files.readAllLines(Path.of("/proc/self/status"))) {
        if (l.startsWith("VmRSS:")) return Long.parseLong(l.replaceAll("\\D", "")) / 1024.0;
      }
    } catch (Exception ignored) {
      // not Linux
    }
    return 0;
  }

  static void serveMetrics(PrometheusMeterRegistry registry, int port) throws Exception {
    HttpServer s = HttpServer.create(new InetSocketAddress(port), 0);
    s.createContext(
        "/metrics",
        ex -> {
          byte[] b = registry.scrape().getBytes(StandardCharsets.UTF_8);
          ex.getResponseHeaders().set("Content-Type", "text/plain; version=0.0.4");
          ex.sendResponseHeaders(200, b.length);
          try (OutputStream o = ex.getResponseBody()) {
            o.write(b);
          }
        });
    s.start();
    log.info("metrics on :" + port + "/metrics");
  }
}
