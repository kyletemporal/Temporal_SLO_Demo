package io.temporal.tuning;

import com.google.common.util.concurrent.AtomicDouble;
import com.uber.m3.tally.Buckets;
import com.uber.m3.tally.Capabilities;
import com.uber.m3.tally.StatsReporter;
import com.uber.m3.util.Duration;
import io.micrometer.core.instrument.Gauge;
import io.micrometer.core.instrument.MeterRegistry;
import io.micrometer.core.instrument.Tags;
import java.util.Map;
import java.util.TreeMap;
import java.util.concurrent.ConcurrentHashMap;

/**
 * Exports SDK gauges to Micrometer so that a gauge which a NEW Worker generation no longer reports
 * disappears, instead of freezing at the old generation's last value.
 *
 * <p>Why this exists, measured: with a plain MicrometerClientStatsReporter, swapping from MANUAL
 * to RESOURCE_BASED left {@code temporal_worker_task_slots_available{worker_type="ActivityWorker"}}
 * at 10 indefinitely. With a WorkerTuner, 1.28.4 stops reporting that series for the
 * ActivityWorker, and a Micrometer gauge holds its last value forever. Prometheus showed 10 free
 * slots while the tuner granted 1 and the backlog grew past 300. A "free slots" panel or a
 * "slots_available == 0" alert would have read healthy for as long as the process lived.
 *
 * <p>Counters and timers pass straight through to the delegate. Gauges are owned here: each
 * remembers the last generation that reported it, and {@link #retire} removes the ones whose last
 * reporter was the retiring generation.
 */
public final class GenerationalGaugeReporter {
  private final MeterRegistry registry;
  private final StatsReporter delegate;
  private final Map<String, Entry> gauges = new ConcurrentHashMap<>();

  private static final class Entry {
    final AtomicDouble value = new AtomicDouble();
    volatile String owner;
    Gauge meter;
  }

  public GenerationalGaugeReporter(MeterRegistry registry, StatsReporter delegate) {
    this.registry = registry;
    this.delegate = delegate;
  }

  /** Removes every gauge last reported by {@code generation}. Call after it has drained. */
  public void retire(String generation) {
    gauges
        .entrySet()
        .removeIf(
            e -> {
              if (!generation.equals(e.getValue().owner)) return false;
              registry.remove(e.getValue().meter);
              return true;
            });
  }

  public StatsReporter forGeneration(String generation) {
    return new StatsReporter() {
      @Override
      public void reportGauge(String name, Map<String, String> tags, double value) {
        Map<String, String> sorted = new TreeMap<>(tags);
        Entry e =
            gauges.computeIfAbsent(
                name + sorted,
                k -> {
                  Entry n = new Entry();
                  Tags t = Tags.empty();
                  for (Map.Entry<String, String> x : sorted.entrySet())
                    t = t.and(x.getKey(), x.getValue());
                  n.meter = Gauge.builder(name, n.value, AtomicDouble::get).tags(t).register(registry);
                  return n;
                });
        e.value.set(value);
        e.owner = generation;
      }

      @Override
      public void reportCounter(String name, Map<String, String> tags, long value) {
        delegate.reportCounter(name, tags, value);
      }

      @Override
      public void reportTimer(String name, Map<String, String> tags, Duration interval) {
        delegate.reportTimer(name, tags, interval);
      }

      @Override
      public void reportHistogramValueSamples(
          String name, Map<String, String> tags, Buckets b, double lo, double hi, long n) {
        delegate.reportHistogramValueSamples(name, tags, b, lo, hi, n);
      }

      @Override
      public void reportHistogramDurationSamples(
          String name, Map<String, String> tags, Buckets b, Duration lo, Duration hi, long n) {
        delegate.reportHistogramDurationSamples(name, tags, b, lo, hi, n);
      }

      @Override
      public Capabilities capabilities() {
        return delegate.capabilities();
      }

      @Override
      public void flush() {
        delegate.flush();
      }

      @Override
      public void close() {
        // Shared delegate: never closed by one generation.
      }
    };
  }
}
