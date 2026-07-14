package org.checkerframework.specimin.usagecontext;

import com.github.javaparser.Range;
import com.github.javaparser.ast.CompilationUnit;
import com.github.javaparser.ast.expr.Expression;
import com.github.javaparser.ast.expr.FieldAccessExpr;
import com.github.javaparser.ast.expr.NameExpr;
import com.github.javaparser.resolution.declarations.ResolvedValueDeclaration;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;

/**
 * For a resolved declaration, finds every real (symbol-resolved) usage site of it across the
 * project and classifies each SAFE or UNSAFE using the checker's own diagnostic output as the
 * oracle. This is the "usage context" idea generalized: instead of a hand-written regex deciding
 * what counts as a null-check guard, we ask the checker itself which usage sites it could and could
 * not already prove safe under the current annotation.
 */
public final class UsageContextCollector {

  private final JavaProjectIndex index;
  private final Set<FileLine> checkerFlaggedLines;
  private final Map<Path, List<String>> lineCache = new HashMap<>();

  public UsageContextCollector(JavaProjectIndex index, Set<FileLine> checkerFlaggedLines) {
    this.index = index;
    this.checkerFlaggedLines = checkerFlaggedLines;
  }

  public List<UsageSite> collect(DeclarationKey target) {
    List<UsageSite> sites = new ArrayList<>();
    for (Map.Entry<Path, CompilationUnit> entry : index.units().entrySet()) {
      Path file = entry.getKey();
      CompilationUnit cu = entry.getValue();

      List<Expression> candidates = new ArrayList<>();
      candidates.addAll(cu.findAll(NameExpr.class));
      candidates.addAll(cu.findAll(FieldAccessExpr.class));

      for (Expression candidate : candidates) {
        ResolvedValueDeclaration resolved;
        try {
          resolved =
              candidate instanceof NameExpr ne
                  ? ne.resolve()
                  : ((FieldAccessExpr) candidate).resolve();
        } catch (RuntimeException e) {
          continue;
        }
        Optional<DeclarationKey> key = DeclarationKey.of(resolved);
        if (key.isEmpty() || !key.get().equals(target)) {
          continue;
        }
        Optional<Range> range = candidate.getRange();
        if (range.isEmpty()) {
          continue;
        }
        int line = range.get().begin.line;
        boolean unsafe = checkerFlaggedLines.contains(new FileLine(file, line));
        sites.add(
            new UsageSite(
                file,
                line,
                lineText(file, line),
                unsafe ? UsageSite.Classification.UNSAFE : UsageSite.Classification.SAFE));
      }
    }
    return sites;
  }

  private String lineText(Path file, int line) {
    List<String> lines =
        lineCache.computeIfAbsent(
            file,
            f -> {
              try {
                return Files.readAllLines(f);
              } catch (IOException e) {
                return List.of();
              }
            });
    return (line >= 1 && line <= lines.size()) ? lines.get(line - 1).strip() : "";
  }
}
