package io.temporal.lab;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import io.temporal.activity.ActivityInterface;
import io.temporal.activity.ActivityMethod;
import io.temporal.activity.ActivityOptions;
import io.temporal.common.RetryOptions;
import io.temporal.workflow.ActivityStub;
import io.temporal.workflow.Promise;
import io.temporal.workflow.Workflow;
import io.temporal.workflow.WorkflowInterface;
import io.temporal.workflow.WorkflowMethod;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;

/**
 * The same Workflow and Activity as the Go and Python lanes (../shared.go). The type names and
 * JSON field names must match exactly: the controller and the k6 test start "LabWorkflow" with
 * {"lane","phases","activitiesPerPhase"}, and Java's default Jackson converter reads the same
 * json/plain payload Go writes.
 */
public final class Lab {
  private Lab() {}

  @JsonIgnoreProperties(ignoreUnknown = true)
  public static class WorkflowInput {
    public String lane = "java";
    public int phases = 3;
    public int activitiesPerPhase = 2;
  }

  @JsonIgnoreProperties(ignoreUnknown = true)
  public static class ActivityInput {
    public String lane;
    public int phase;
    public int index;

    public ActivityInput() {}

    ActivityInput(String lane, int phase, int index) {
      this.lane = lane;
      this.phase = phase;
      this.index = index;
    }
  }

  @WorkflowInterface
  public interface LabWorkflow {
    // name= is required: Java defaults the type to the interface's simple name, which happens
    // to match here, but the method-level name pins it against a rename.
    @WorkflowMethod(name = "LabWorkflow")
    int run(WorkflowInput in);
  }

  @ActivityInterface
  public interface LabActivity {
    // Without name=, Java registers the Activity as "Run" (method name, capitalised), and every
    // Activity the Go-shaped Workflow schedules would sit unmatched on the queue forever.
    @ActivityMethod(name = "LabActivity")
    void run(ActivityInput in);
  }

  public static class LabWorkflowImpl implements LabWorkflow {
    // An UNTYPED stub with executeAsync, not Async.function on a typed stub: no extra Workflow
    // thread per parallel Activity, so the thread-pool numbers reflect cached Workflows only.
    private final ActivityStub act =
        Workflow.newUntypedActivityStub(
            ActivityOptions.newBuilder()
                .setStartToCloseTimeout(Duration.ofMinutes(5))
                .setRetryOptions(
                    RetryOptions.newBuilder()
                        .setInitialInterval(Duration.ofSeconds(1))
                        .setMaximumAttempts(3)
                        .build())
                .build());

    @Override
    public int run(WorkflowInput in) {
      int done = 0;
      for (int p = 0; p < in.phases; p++) {
        List<Promise<Void>> futures = new ArrayList<>(in.activitiesPerPhase);
        for (int i = 0; i < in.activitiesPerPhase; i++) {
          futures.add(
              act.executeAsync("LabActivity", Void.class, new ActivityInput(in.lane, p, i)));
        }
        for (Promise<Void> f : futures) {
          f.get();
          done++;
        }
      }
      return done;
    }
  }
}
