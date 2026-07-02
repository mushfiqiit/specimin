package org.checkerframework.specimin.usagecontext;

import com.github.javaparser.JavaParser;
import com.github.javaparser.ParserConfiguration;
import com.github.javaparser.ast.CompilationUnit;
import com.github.javaparser.ast.body.ClassOrInterfaceDeclaration;
import com.github.javaparser.ast.body.FieldDeclaration;
import com.github.javaparser.ast.expr.MarkerAnnotationExpr;
import com.github.javaparser.printer.lexicalpreservation.LexicalPreservingPrinter;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Optional;

/**
 * Rewrites a field's annotation using a format-preserving AST edit -- the same technique already
 * used by {@code ApplyAnnotations.java} for the LLM pipeline, generalized to an arbitrary {@link
 * Property} instead of a hardcoded Nullable/Nonnull pair, and driven by a symbol-resolved {@link
 * DeclarationKey} instead of a bare field-name string match.
 */
public final class AstAnnotationRewriter {

  private AstAnnotationRewriter() {}

  /**
   * @param file the file that declares {@code key} (from {@link
   *     JavaProjectIndex#fileDeclaringType})
   * @return true if the annotation was (or, in dry-run mode, would be) rewritten
   */
  public static boolean rewriteFieldAnnotation(
      Path file, DeclarationKey key, Property property, boolean dryRun, boolean verbose)
      throws IOException {
    ParserConfiguration lpConfig = new ParserConfiguration();
    lpConfig.setLexicalPreservationEnabled(true);
    JavaParser lpParser = new JavaParser(lpConfig);
    CompilationUnit cu = lpParser.parse(file).getResult().orElse(null);
    if (cu == null) {
      return false;
    }
    LexicalPreservingPrinter.setup(cu);

    for (ClassOrInterfaceDeclaration type : cu.findAll(ClassOrInterfaceDeclaration.class)) {
      Optional<String> fqn = type.getFullyQualifiedName();
      if (fqn.isEmpty() || !fqn.get().equals(key.qualifiedOwner)) {
        continue;
      }
      for (FieldDeclaration field : type.getFields()) {
        boolean nameMatches =
            field.getVariables().stream().anyMatch(v -> v.getNameAsString().equals(key.memberName));
        if (!nameMatches) {
          continue;
        }
        if (!field.isAnnotationPresent(property.weakerAnnotation())) {
          return false; // Nothing to flip -- may already have been fixed.
        }
        field
            .getAnnotations()
            .removeIf(a -> a.getNameAsString().equals(property.weakerAnnotation()));
        field.getAnnotations().add(new MarkerAnnotationExpr(property.strongerAnnotation()));
        if (property.strongerAnnotationImport() != null) {
          cu.addImport(property.strongerAnnotationImport());
        }
        if (verbose) {
          System.out.println(
              "  ["
                  + key
                  + "] @"
                  + property.weakerAnnotation()
                  + " -> @"
                  + property.strongerAnnotation());
        }
        if (!dryRun) {
          Files.writeString(file, LexicalPreservingPrinter.print(cu));
        }
        return true;
      }
    }
    return false;
  }
}
