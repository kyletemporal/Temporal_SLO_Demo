package io.temporal.tuning;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import io.temporal.worker.WorkerOptions;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

/** Each test is one landmine. Its name says what goes wrong if the rule is removed. */
class TuningFlagTest {
  static final String SDK = "1.28.4";

  static boolean any(List<String> l, String s) {
    return l.stream().anyMatch(x -> x.contains(s));
  }

  @Test
  void typoInFieldNameIsAnErrorNotASilentDefault() {
    // "activityPollers" is not a field. Lenient parsing would leave activityTaskPollers at its
    // default and report success.
    assertThrows(Exception.class, () -> TuningFlag.parse("{\"mode\":\"MANUAL\",\"activityPollers\":8}"));
  }

  @Test
  void zeroMeansSdkDefaultSoItIsRejected() {
    TuningFlag f = new TuningFlag();
    f.activityTaskPollers = 0;
    assertTrue(any(f.validate(SDK).errors, "treats 0 as 'use the default'"));
  }

  @Test
  void oneWorkflowPollerBecomesTwo() {
    TuningFlag f = new TuningFlag();
    f.workflowTaskPollers = 1;
    assertTrue(f.validate(SDK).ok());
    assertTrue(any(f.validate(SDK).warnings, "raised to 2"));
    assertEquals(2, f.effective().get("workflow_task_pollers"));
  }

  @Test
  void pollersBeyondSlotsNeverPoll() {
    TuningFlag f = new TuningFlag();
    f.activityTaskPollers = 16;
    f.activitySlots = 4;
    assertTrue(any(f.validate(SDK).warnings, "never poll"));
  }

  @Test
  void cacheLargerThanThreadPoolIsFlagged() {
    TuningFlag f = new TuningFlag();
    f.stickyCacheSize = 1000;
    f.maxWorkflowThreads = 600;
    assertTrue(any(f.validate(SDK).warnings, "parks a thread"));
  }

  @Test
  void resourceTunerRefusedOnVersionsWithTheHangBug() {
    TuningFlag f = new TuningFlag();
    f.mode = TuningFlag.Mode.RESOURCE_BASED;
    assertTrue(f.validate("1.28.4").ok());
    assertFalse(f.validate("1.29.0").ok());
    assertFalse(f.validate("1.32.1").ok());
    assertTrue(f.validate("1.33.0").ok());
    f.acceptKnownTunerHangBug = true;
    assertTrue(f.validate("1.29.0").ok());
  }

  @Test
  void resourceModeBuildsWithoutTrippingWorkerOptionsChecks() {
    // WorkerOptions throws if a tuner is combined with setMaxConcurrent*ExecutionSize.
    TuningFlag f = new TuningFlag();
    f.mode = TuningFlag.Mode.RESOURCE_BASED;
    WorkerOptions o = f.workerOptions();
    assertTrue(o.getWorkerTuner() != null);
    assertEquals(f.activityTaskPollers, o.getMaxConcurrentActivityTaskPollers());
  }

  @Test
  void manualModeSetsEveryValue() {
    TuningFlag f = new TuningFlag();
    f.workflowTaskPollers = 3;
    f.activityTaskPollers = 7;
    f.workflowTaskSlots = 11;
    f.activitySlots = 13;
    WorkerOptions o = f.workerOptions();
    assertEquals(3, o.getMaxConcurrentWorkflowTaskPollers());
    assertEquals(7, o.getMaxConcurrentActivityTaskPollers());
    assertEquals(11, o.getMaxConcurrentWorkflowTaskExecutionSize());
    assertEquals(13, o.getMaxConcurrentActivityExecutionSize());
  }

  @Test
  void labelOnlyChangeDoesNotSwap() {
    TuningFlag a = new TuningFlag(), b = new TuningFlag();
    b.label = "CHG-1234";
    assertTrue(a.sameWorkerAs(b));
    b.activitySlots++;
    assertFalse(a.sameWorkerAs(b));
  }

  @Test
  void envFlagParses() {
    TuningFlag f =
        TuningFlag.fromEnv(Map.of("TUNING_MODE", "manual", "TUNING_ACT_POLLERS", "9"));
    assertEquals(TuningFlag.Mode.MANUAL, f.mode);
    assertEquals(9, f.activityTaskPollers);
  }

  @Test
  void fileSourceSubmitsOnlyOnContentChangeAndSkipsBadFiles(@TempDir Path dir) throws Exception {
    Path p = dir.resolve("tuning.json");
    List<TuningFlag> got = new ArrayList<>();
    FileFlagSource src = new FileFlagSource(p, got::add);
    assertFalse(src.check()); // missing: keep current
    Files.writeString(p, "{\"mode\":\"MANUAL\",\"activityTaskPollers\":4}");
    assertTrue(src.check());
    assertFalse(src.check()); // same content: no resubmit, no swap
    Files.writeString(p, "{\"mode\":\"MANUAL\",\"activityTaskPoll"); // half-written
    assertFalse(src.check());
    Files.writeString(p, "{\"mode\":\"MANUAL\",\"activityTaskPollers\":8}");
    assertTrue(src.check());
    assertEquals(2, got.size());
    assertEquals(8, got.get(1).activityTaskPollers);
  }
}
