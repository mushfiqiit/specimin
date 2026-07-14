package org.checkerframework.specimin.usagecontext;

import java.util.Objects;
import java.util.Optional;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/** The nullability property: NullAway's "dereferenced expression X is @Nullable" warnings. */
public final class NullnessProperty implements Property {

  private static final Pattern DEREF =
      Pattern.compile("dereferenced expression\\s+(.+?)\\s+is\\s+@Nullable");

  @Override
  public String checkerKey() {
    return "NullAway";
  }

  @Override
  public String weakerAnnotation() {
    return "Nullable";
  }

  @Override
  public String strongerAnnotation() {
    return "Nonnull";
  }

  @Override
  public String strongerAnnotationImport() {
    return "javax.annotation.Nonnull";
  }

  @Override
  public boolean isCandidateWarning(Diagnostic d) {
    return d.checkerKey.equals(checkerKey()) && DEREF.matcher(d.message).find();
  }

  @Override
  public Optional<String> extractUsageExpression(Diagnostic d) {
    Matcher m = DEREF.matcher(d.message);
    if (!m.find()) {
      return Optional.empty();
    }
    // Group 1 is a mandatory (non-optional) capturing group, so it always matches once
    // m.find() succeeds; requireNonNull just tells the Nullness Checker that.
    String expr = Objects.requireNonNull(m.group(1)).trim();
    int dot = expr.lastIndexOf('.');
    return Optional.of(dot >= 0 ? expr.substring(dot + 1) : expr);
  }
}
