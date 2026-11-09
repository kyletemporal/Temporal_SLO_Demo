package io.temporal.lab;

import com.uber.m3.tally.Buckets;
import com.uber.m3.tally.Capabilities;
import com.uber.m3.tally.StatsReporter;
import com.uber.m3.util.Duration;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;

/**
 * Tees the SDK's tally metrics: everything still goes to Micrometer/Prometheus, and the canonical
 * subset (see ../telemetry.go) is aggregated in-process for the live UI. This is the Java twin of
 * the Go teeHandler.
 *
 * <p>Java-specific mappings beyond Go's: the Workflow THREAD pool. A Java Worker can be starved of
 * Workflow threads with free slots and an idle CPU, and the evidence lives only in
 * temporal_sticky_cache_thread_forced_eviction and temporal_workflow_active_thread_count.
 */
final class Telemetry {
  static final Map<String, String> TIMERS =
      Map.of(
          "workflow_task_schedule_to_start_latency", "wft_s2s",
          "workflow_task_execution_latency", "wft_exec",
          "activity_schedule_to_start_latency", "act_s2s",
          "activity_execution_latency", "act_exec",
          "workflow_task_replay_latency", "replay",
          "workflow_endtoend_latency", "wf_e2e");
  static final Map<String, String> TIMER_COUNTS =
      Map.of("wft_exec", "wft_completed", "act_exec", "act_completed");
  static final Map<String, String> COUNTERS =
      Map.of(
          "workflow_completed", "wf_completed",
          "activity_execution_failed", "act_failed",
          "sticky_cache_hit", "cache_hit",
          "sticky_cache_miss", "cache_miss",
          "sticky_cache_thread_forced_eviction", "cache_thread_evict",
          "sticky_cache_total_forced_eviction", "cache_forced_evict");

  static final double WINDOW_S = 10;

  final Map<String, Double> counters = new ConcurrentHashMap<>();
  // instance -> key -> value: during a swap both generations' gauges are summed, and a retired
  // generation is dropped rather than lingering at its last value.
  final Map<String, Map<String, Double>> gauges = new ConcurrentHashMap<>();
  private final Map<String, ArrayDeque<double[]>> samples = new HashMap<>();
  final Set<String> seenNames = ConcurrentHashMap.newKeySet();

  static String canonical(String name) {
    return name.startsWith("temporal_") ? name.substring("temporal_".length()) : name;
  }

  static String gaugeKey(String name, Map<String, String> tags) {
    switch (name) {
      case "worker_task_slots_used":
      case "worker_task_slots_available":
        String kind =
            Map.of("WorkflowWorker", "wft", "ActivityWorker", "act")
                .get(tags.getOrDefault("worker_type", ""));
        return kind == null ? null : kind + "_slots_" + name.substring("worker_task_slots_".length());
      case "num_pollers": // 1.30+ only. On 1.2x it never arrives.
        switch (tags.getOrDefault("poller_type", "")) {
          case "workflow_task":
            return "pollers_workflow";
          case "workflow_sticky_task":
            return "pollers_sticky";
          case "activity_task":
            return "pollers_activity";
          default:
            return null;
        }
      case "sticky_cache_size":
        return "cache_size";
      case "workflow_active_thread_count":
        return "wf_threads";
      // ResourceBasedTuner's view: SYSTEM cpu (0-1) and heap/-Xmx. No temporal_ prefix before
      // 1.32 (sdk-java #2710), so a dashboard querying temporal_resource_slots_* finds nothing.
      case "resource_slots_cpu_usage":
        return "tuner_cpu";
      case "resource_slots_mem_usage":
        return "tuner_mem";
      default:
        return null;
    }
  }

  static String pollKey(String op) {
    if (op == null) return null;
    if (op.contains("PollActivityTaskQueue")) return "poll_act";
    if (op.contains("PollWorkflowTaskQueue")) return "poll_wft";
    return null;
  }

  void inc(String key, double n) {
    counters.merge(key, n, Double::sum);
  }

  synchronized void record(String key, double seconds) {
    double now = System.nanoTime() / 1e9;
    ArrayDeque<double[]> q = samples.computeIfAbsent(key, k -> new ArrayDeque<>());
    q.addLast(new double[] {now, seconds});
    while (q.size() > 20000) q.removeFirst();
    String c = TIMER_COUNTS.get(key);
    if (c != null) inc(c, 1);
  }

  void dropInstance(String instance) {
    gauges.remove(instance);
  }

  Map<String, Double> summedGauges() {
    Map<String, Double> out = new HashMap<>();
    for (Map<String, Double> g : gauges.values()) g.forEach((k, v) -> out.merge(k, v, Double::sum));
    return out;
  }

  /** p50/p95/mean/n per key over the last WINDOW_S seconds, in seconds. */
  synchronized Map<String, Map<String, Object>> latency() {
    double cutoff = System.nanoTime() / 1e9 - WINDOW_S;
    Map<String, Map<String, Object>> out = new HashMap<>();
    for (Map.Entry<String, ArrayDeque<double[]>> e : samples.entrySet()) {
      ArrayDeque<double[]> q = e.getValue();
      while (!q.isEmpty() && q.peekFirst()[0] < cutoff) q.removeFirst();
      List<Double> v = new ArrayList<>(q.size());
      double sum = 0;
      for (double[] s : q) {
        v.add(s[1]);
        sum += s[1];
      }
      Collections.sort(v);
      Map<String, Object> p = new HashMap<>();
      p.put("p50", v.isEmpty() ? 0 : v.get((int) (0.50 * (v.size() - 1))));
      p.put("p95", v.isEmpty() ? 0 : v.get((int) (0.95 * (v.size() - 1))));
      p.put("mean", v.isEmpty() ? 0 : sum / v.size());
      p.put("n", v.size());
      out.put(e.getKey(), p);
    }
    return out;
  }

  /** A tally reporter for one Worker generation, forwarding everything to {@code next}. */
  StatsReporter tee(StatsReporter next, String instance) {
    return new StatsReporter() {
      @Override
      public void reportCounter(String name, Map<String, String> tags, long value) {
        next.reportCounter(name, tags, value);
        String n = canonical(name);
        seenNames.add(n);
        // tally reports counters as the DELTA since the last report, not a running total.
        String k = COUNTERS.get(n);
        if (k != null) inc(k, value);
      }

      @Override
      public void reportGauge(String name, Map<String, String> tags, double value) {
        next.reportGauge(name, tags, value);
        String n = canonical(name);
        seenNames.add(n);
        String k = gaugeKey(n, tags);
        if (k != null) gauges.computeIfAbsent(instance, i -> new ConcurrentHashMap<>()).put(k, value);
      }

      @Override
      public void reportTimer(String name, Map<String, String> tags, Duration interval) {
        next.reportTimer(name, tags, interval);
        String n = canonical(name);
        seenNames.add(n);
        String k =
            n.equals("long_request_latency") ? pollKey(tags.get("operation")) : TIMERS.get(n);
        if (k != null) record(k, interval.getNanos() / 1e9);
      }

      @Override
      public void reportHistogramValueSamples(
          String name,
          Map<String, String> tags,
          Buckets buckets,
          double lo,
          double hi,
          long samples) {
        next.reportHistogramValueSamples(name, tags, buckets, lo, hi, samples);
      }

      @Override
      public void reportHistogramDurationSamples(
          String name,
          Map<String, String> tags,
          Buckets buckets,
          Duration lo,
          Duration hi,
          long samples) {
        next.reportHistogramDurationSamples(name, tags, buckets, lo, hi, samples);
      }

      @Override
      public Capabilities capabilities() {
        return next.capabilities();
      }

      @Override
      public void flush() {
        next.flush();
      }

      @Override
      public void close() {
        // The Micrometer reporter is shared across generations; never close it here.
      }
    };
  }
}
