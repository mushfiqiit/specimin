package org.checkerframework.specimin.usagecontext;

import java.nio.file.Path;

/** One usage site of a declaration, classified against the checker's own diagnostic output. */
public final class UsageSite {

  /**
   * UNSAFE = the checker still has to warn at this site under the current annotation (real evidence
   * the current annotation is correct there). SAFE = the checker's own flow analysis already proves
   * the stronger property holds at this site (a guard, a definite assignment, etc.) without any
   * hand-written "is this a null-check" heuristic -- the checker is the oracle.
   */
  public enum Classification {
    SAFE,
    UNSAFE
  }

  public final Path file;
  public final int line;
  public final String snippet;
  public final Classification classification;

  public UsageSite(Path file, int line, String snippet, Classification classification) {
    this.file = file;
    this.line = line;
    this.snippet = snippet;
    this.classification = classification;
  }
}
