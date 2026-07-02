package org.checkerframework.specimin.usagecontext;

import java.util.List;

/** Turns a declaration's SAFE/UNSAFE usage sites into a decision. */
public final class EvidenceAggregator {

  /** What to do about a candidate spurious annotation, given its usage-context evidence. */
  public enum Verdict {
    /** Every real usage site needs the stronger property -- safe to auto-rewrite. */
    REWRITE_HIGH_CONFIDENCE,
    /** Mixed evidence: some sites need the stronger property, some rely on the weaker one. */
    AMBIGUOUS_NEEDS_LLM,
    /** No usage site contradicts the current annotation. */
    KEEP
  }

  private EvidenceAggregator() {}

  public static Verdict aggregate(List<UsageSite> sites) {
    long unsafe =
        sites.stream().filter(s -> s.classification == UsageSite.Classification.UNSAFE).count();
    long safe =
        sites.stream().filter(s -> s.classification == UsageSite.Classification.SAFE).count();

    if (unsafe == 0) {
      return Verdict.KEEP;
    }
    if (safe == 0) {
      return Verdict.REWRITE_HIGH_CONFIDENCE;
    }
    return Verdict.AMBIGUOUS_NEEDS_LLM;
  }
}
