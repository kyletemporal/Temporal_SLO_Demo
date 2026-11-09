package io.temporal.tuning;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.NoSuchFileException;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.util.HexFormat;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import java.util.function.Consumer;
import java.util.logging.Logger;

/**
 * Watches a JSON flag file and submits each new value.
 *
 * <p>It compares CONTENT HASHES, not modification times or WatchService events. A Kubernetes
 * ConfigMap volume updates by swapping a {@code ..data} symlink. The file's own mtime and inode do
 * not change the way a watcher expects, so a WatchService on the file sees nothing, ever.
 *
 * <p>Two Kubernetes traps this class cannot fix, so check them on your side:
 *
 * <ul>
 *   <li>A ConfigMap mounted with {@code subPath} is NEVER updated in a running pod. The flag file
 *       stays at its boot value until the pod restarts.
 *   <li>Kubelet propagates ConfigMap changes with up to about a minute of delay (sync period plus
 *       cache TTL). A flip is not instant, and pods pick it up at different times.
 * </ul>
 *
 * <p>A missing or unparsable file keeps the current config and is logged. It never resets to
 * defaults.
 */
public final class FileFlagSource implements AutoCloseable {
  private static final Logger log = Logger.getLogger(FileFlagSource.class.getName());
  private final Path path;
  private final Consumer<TuningFlag> sink;
  private final ScheduledExecutorService exec = Executors.newSingleThreadScheduledExecutor();
  private String lastHash = "";
  private String lastBadHash = "";
  private boolean missingLogged;

  public FileFlagSource(Path path, Consumer<TuningFlag> sink) {
    this.path = path;
    this.sink = sink;
  }

  public FileFlagSource start(long periodMs) {
    exec.scheduleWithFixedDelay(this::check, 0, periodMs, TimeUnit.MILLISECONDS);
    return this;
  }

  /** Visible for tests: one read-compare-submit cycle. Returns true if a new value was submitted. */
  boolean check() {
    try {
      byte[] b = Files.readAllBytes(path);
      String h = HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(b));
      if (h.equals(lastHash) || h.equals(lastBadHash)) return false;
      TuningFlag f;
      try {
        f = TuningFlag.parse(new String(b, StandardCharsets.UTF_8));
      } catch (Exception e) {
        // Log a bad file once, not every tick; a rewrite (new hash) is tried again.
        lastBadHash = h;
        log.warning("flag file " + path + " unparsable, keeping current config: " + e.getMessage());
        return false;
      }
      lastHash = h;
      sink.accept(f);
      return true;
    } catch (NoSuchFileException e) {
      if (!missingLogged) log.warning("flag file " + path + " missing; keeping current config");
      missingLogged = true;
    } catch (Exception e) {
      log.warning("flag file " + path + " unreadable, keeping current config: " + e);
    }
    return false;
  }

  @Override
  public void close() {
    exec.shutdownNow();
  }
}
