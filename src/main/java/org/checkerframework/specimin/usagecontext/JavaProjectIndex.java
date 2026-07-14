package org.checkerframework.specimin.usagecontext;

import com.github.javaparser.JavaParser;
import com.github.javaparser.ParserConfiguration;
import com.github.javaparser.Range;
import com.github.javaparser.ast.CompilationUnit;
import com.github.javaparser.ast.body.ClassOrInterfaceDeclaration;
import com.github.javaparser.ast.expr.Expression;
import com.github.javaparser.ast.expr.FieldAccessExpr;
import com.github.javaparser.ast.expr.NameExpr;
import com.github.javaparser.resolution.declarations.ResolvedValueDeclaration;
import com.github.javaparser.symbolsolver.JavaSymbolSolver;
import com.github.javaparser.symbolsolver.resolution.typesolvers.CombinedTypeSolver;
import com.github.javaparser.symbolsolver.resolution.typesolvers.JavaParserTypeSolver;
import com.github.javaparser.symbolsolver.resolution.typesolvers.ReflectionTypeSolver;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.stream.Stream;

/**
 * Parses every {@code .java} file under a source root with a configured symbol solver. This is the
 * AST front end that replaces the regex/brace-scanning "parsing" used throughout the old Python
 * pipeline -- declarations and usage sites here are real, symbol-resolved AST nodes.
 */
public final class JavaProjectIndex {

  private final Map<Path, CompilationUnit> unitsByFile = new LinkedHashMap<>();

  public JavaProjectIndex(Path srcRoot) throws IOException {
    CombinedTypeSolver typeSolver = new CombinedTypeSolver();
    typeSolver.add(new ReflectionTypeSolver());
    typeSolver.add(new JavaParserTypeSolver(srcRoot));
    JavaSymbolSolver symbolSolver = new JavaSymbolSolver(typeSolver);

    ParserConfiguration config = new ParserConfiguration();
    config.setSymbolResolver(symbolSolver);
    JavaParser parser = new JavaParser(config);

    List<Path> javaFiles;
    try (Stream<Path> walk = Files.walk(srcRoot)) {
      javaFiles = walk.filter(p -> p.toString().endsWith(".java")).toList();
    }
    for (Path file : javaFiles) {
      Path normalized = PathNormalizer.normalize(file);
      parser.parse(file).getResult().ifPresent(cu -> unitsByFile.put(normalized, cu));
    }
  }

  public Map<Path, CompilationUnit> units() {
    return unitsByFile;
  }

  /** The file that declares the given fully-qualified type name, if it was indexed. */
  public Optional<Path> fileDeclaringType(String qualifiedTypeName) {
    for (Map.Entry<Path, CompilationUnit> e : unitsByFile.entrySet()) {
      for (ClassOrInterfaceDeclaration type :
          e.getValue().findAll(ClassOrInterfaceDeclaration.class)) {
        if (type.getFullyQualifiedName().map(qualifiedTypeName::equals).orElse(false)) {
          return Optional.of(e.getKey());
        }
      }
    }
    return Optional.empty();
  }

  /**
   * Finds the expression on {@code diagnostic.line} in {@code diagnostic.file} whose last dotted
   * component matches {@code expressionHint}, and resolves it to its declaration. This replaces the
   * old approach of splitting the warning's expression text on "." and treating the last component
   * as *the* field name everywhere in the codebase -- here the match is verified by real symbol
   * resolution, so same-named fields in unrelated classes are never confused.
   */
  public Optional<ResolvedValueDeclaration> resolveDeclaration(
      Diagnostic diagnostic, String expressionHint) {
    CompilationUnit cu = unitsByFile.get(diagnostic.file);
    if (cu == null) {
      return Optional.empty();
    }
    List<Expression> candidates = new ArrayList<>();
    candidates.addAll(cu.findAll(NameExpr.class));
    candidates.addAll(cu.findAll(FieldAccessExpr.class));

    for (Expression candidate : candidates) {
      Optional<Range> range = candidate.getRange();
      if (range.isEmpty() || range.get().begin.line != diagnostic.line) {
        continue;
      }
      String text = candidate.toString();
      String lastComponent = text.contains(".") ? text.substring(text.lastIndexOf('.') + 1) : text;
      if (!lastComponent.equals(expressionHint)) {
        continue;
      }
      try {
        if (candidate instanceof NameExpr ne) {
          return Optional.of(ne.resolve());
        } else if (candidate instanceof FieldAccessExpr fae) {
          return Optional.of(fae.resolve());
        }
      } catch (RuntimeException ignored) {
        // Keep looking -- another candidate on the same line may resolve.
      }
    }
    return Optional.empty();
  }
}
