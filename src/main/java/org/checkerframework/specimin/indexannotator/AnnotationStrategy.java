package org.checkerframework.specimin.indexannotator;

import com.github.javaparser.ast.CompilationUnit;

/**
 * One heuristic that scans a parsed source file for a specific Index Checker warning pattern and
 * inserts the annotation(s) that resolve it. Implementations mutate {@code cu} in place (the
 * caller sets up {@link com.github.javaparser.printer.lexicalpreservation.LexicalPreservingPrinter}
 * so the edits can be printed back as a minimal, format-preserving diff) and report how many
 * insertions they made so the caller can decide whether the file changed.
 *
 * <p>These are heuristics, not a sound analysis: each one targets the common-case shape of one of
 * the patterns observed in gson's real index-checker-warnings.log, not an exhaustive semantic
 * proof. The goal is minimizing warnings, not eliminating every one -- see each strategy's class
 * Javadoc for what it does and does not handle.
 */
interface AnnotationStrategy {

  /** Short name used in the tool's report output. */
  String name();

  /**
   * Scans {@code cu} for this strategy's pattern and inserts annotations in place.
   *
   * @param cu the compilation unit to scan and edit
   * @return the number of annotations inserted
   */
  int apply(CompilationUnit cu);
}
