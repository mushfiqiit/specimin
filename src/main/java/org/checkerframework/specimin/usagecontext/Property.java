package org.checkerframework.specimin.usagecontext;

import java.util.Optional;

/**
 * A pluggable per-checker-property strategy. Implement one of these to add a new inference domain
 * (e.g. array-index bounds via the Index Checker) without touching any of the AST resolution,
 * evidence collection, or aggregation logic elsewhere in this package -- none of those classes
 * mention "null" or any other property by name.
 */
public interface Property {

  /**
   * The Checker Framework / Error Prone diagnostic key, e.g. {@code "NullAway"} or {@code "index"}.
   */
  String checkerKey();

  /**
   * The annotation a warning claims is required, which may in fact be spurious, e.g. {@code
   * "Nullable"}.
   */
  String weakerAnnotation();

  /**
   * The annotation to switch to when evidence says the weaker one is spurious, e.g. {@code
   * "Nonnull"}.
   */
  String strongerAnnotation();

  /**
   * Fully-qualified import for {@link #strongerAnnotation()}, or {@code null} if none is needed.
   */
  String strongerAnnotationImport();

  /** Does this diagnostic belong to this property (right checker key + message shape)? */
  boolean isCandidateWarning(Diagnostic d);

  /**
   * Extracts the source text of the expression a warning is about, e.g. for "dereferenced
   * expression subscription is @Nullable" this returns {@code "subscription"}. Used only to pick a
   * starting candidate AST node on the diagnostic's line; the resolver still verifies the match
   * semantically via the symbol solver, so this never has to be exact for dotted expressions.
   */
  Optional<String> extractUsageExpression(Diagnostic d);
}
