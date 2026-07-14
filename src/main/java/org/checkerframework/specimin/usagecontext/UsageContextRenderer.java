package org.checkerframework.specimin.usagecontext;

import java.util.List;

/**
 * Renders a declaration's usage-context evidence as a human-readable text block, for the {@code
 * AMBIGUOUS_NEEDS_LLM} case in {@link UsageContextInference}: when the checker's own evidence is
 * mixed (some usage sites are SAFE, some UNSAFE), this prints every site instead of guessing, so
 * the LLM inference step can weigh them.
 */
public final class UsageContextRenderer {

  private UsageContextRenderer() {}

  /**
   * Renders {@code sites} for {@code key} as one line per usage, each showing its SAFE/UNSAFE
   * classification, source location, and snippet.
   */
  public static String render(DeclarationKey key, List<UsageSite> sites) {
    StringBuilder rendered = new StringBuilder();
    rendered.append("  usage-context for ").append(key).append(':').append('\n');
    for (UsageSite site : sites) {
      rendered
          .append("    [")
          .append(site.classification)
          .append("] ")
          .append(site.file.getFileName())
          .append(':')
          .append(site.line)
          .append("  ")
          .append(site.snippet)
          .append('\n');
    }
    return rendered.toString();
  }
}
