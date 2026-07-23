package org.checkerframework.specimin.indexannotator;

import com.github.javaparser.StaticJavaParser;
import com.github.javaparser.ast.CompilationUnit;
import com.github.javaparser.ast.body.MethodDeclaration;
import com.github.javaparser.ast.body.Parameter;
import com.github.javaparser.ast.body.VariableDeclarator;
import com.github.javaparser.ast.expr.ArrayAccessExpr;
import com.github.javaparser.ast.expr.Expression;
import com.github.javaparser.ast.expr.NameExpr;
import com.github.javaparser.ast.expr.VariableDeclarationExpr;
import com.github.javaparser.ast.nodeTypes.NodeWithAnnotations;
import com.github.javaparser.ast.stmt.ForStmt;
import com.github.javaparser.ast.type.Type;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Optional;
import java.util.Set;

/**
 * Heuristic 7 (correlated-array length inference), generalized: rather than a hardcoded table of
 * "these two reflection methods return same-length arrays" (which only helps if the exact method
 * pair has been seen before), this looks for any {@code for} loop that indexes two or more
 * different array-typed bindings (parameters or local variables) with the same loop counter --
 * {@code for (int i = 0; i < a.length; i++) { ... a[i] ... b[i] ... }} -- and treats that as
 * evidence {@code a} and {@code b} are the same length, annotating one relative to the other.
 *
 * <p>This deliberately does not attempt to correlate two arrays that are never actually co-indexed
 * in a loop (e.g. gson's own {@code rawType.getGenericInterfaces()[i]} case, where the paired call
 * is inlined rather than assigned to a local first) -- extending this to recognize specific
 * "these two method calls are known to correspond" pairs, or to look through such inlined
 * expressions, is a natural next step but isn't attempted here.
 */
final class SameLenCorrelationStrategy implements AnnotationStrategy {

  @Override
  public String name() {
    return "samelen-correlation";
  }

  @Override
  public int apply(CompilationUnit cu) {
    int inserted = 0;
    for (MethodDeclaration method : cu.findAll(MethodDeclaration.class)) {
      for (ForStmt loop : method.findAll(ForStmt.class)) {
        inserted += annotateCoIndexedArrays(cu, method, loop);
      }
    }
    return inserted;
  }

  private static int annotateCoIndexedArrays(CompilationUnit cu, MethodDeclaration method, ForStmt loop) {
    Optional<String> counter = loopCounterName(loop);
    if (counter.isEmpty()) {
      return 0;
    }
    Set<String> arrayNames = new LinkedHashSet<>();
    for (ArrayAccessExpr access : loop.findAll(ArrayAccessExpr.class)) {
      if (access.getIndex() instanceof NameExpr index
          && index.getNameAsString().equals(counter.get())
          && access.getName() instanceof NameExpr array) {
        arrayNames.add(array.getNameAsString());
      }
    }
    if (arrayNames.size() < 2) {
      return 0;
    }
    List<String> names = List.copyOf(arrayNames);
    String first = names.get(0);
    int inserted = 0;
    for (String other : names.subList(1, names.size())) {
      Optional<Type> declaredType = findDeclaredType(method, other);
      if (declaredType.isPresent() && annotateSameLen(cu, declaredType.get(), first)) {
        inserted++;
      }
    }
    return inserted;
  }

  /** The loop counter name for {@code for (int i = 0; ...; ...)}-shaped loops. */
  private static Optional<String> loopCounterName(ForStmt loop) {
    for (Expression init : loop.getInitialization()) {
      if (init instanceof VariableDeclarationExpr varDecl) {
        for (VariableDeclarator v : varDecl.getVariables()) {
          return Optional.of(v.getNameAsString());
        }
      }
    }
    return Optional.empty();
  }

  private static Optional<Type> findDeclaredType(MethodDeclaration method, String name) {
    for (Parameter param : method.getParameters()) {
      if (param.getNameAsString().equals(name)) {
        return Optional.of(param.getType());
      }
    }
    for (VariableDeclarator v : method.findAll(VariableDeclarator.class)) {
      if (v.getNameAsString().equals(name)) {
        return Optional.of(v.getType());
      }
    }
    return Optional.empty();
  }

  private static boolean annotateSameLen(CompilationUnit cu, Type target, String other) {
    if (!(target instanceof NodeWithAnnotations)
        || target.getAnnotations().stream().anyMatch(a -> a.getNameAsString().equals("SameLen"))) {
      return false;
    }
    ((NodeWithAnnotations<?>) target)
        .addAnnotation(StaticJavaParser.parseAnnotation("@SameLen(\"" + other + "\")"));
    cu.addImport("org.checkerframework.checker.index.qual.SameLen");
    return true;
  }
}
