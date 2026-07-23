package org.checkerframework.specimin.indexannotator;

import com.github.javaparser.StaticJavaParser;
import com.github.javaparser.ast.CompilationUnit;
import com.github.javaparser.ast.body.ClassOrInterfaceDeclaration;
import com.github.javaparser.ast.body.FieldDeclaration;
import com.github.javaparser.ast.body.VariableDeclarator;
import com.github.javaparser.ast.expr.ArrayAccessExpr;
import com.github.javaparser.ast.expr.BinaryExpr;
import com.github.javaparser.ast.expr.Expression;
import com.github.javaparser.ast.expr.FieldAccessExpr;
import com.github.javaparser.ast.expr.IntegerLiteralExpr;
import com.github.javaparser.ast.expr.NameExpr;
import com.github.javaparser.ast.expr.ThisExpr;
import com.github.javaparser.ast.expr.UnaryExpr;
import com.github.javaparser.ast.nodeTypes.NodeWithAnnotations;
import com.github.javaparser.ast.type.PrimitiveType;
import com.github.javaparser.ast.type.Type;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.function.Predicate;

/**
 * Heuristics 1 and 2, merged: a class that maintains an array plus a counter field bounding it
 * (the {@code JsonReader}/{@code JsonWriter}/{@code JsonTreeReader} {@code stack}/{@code
 * stackSize} pattern), and/or a buffer plus a {@code pos}/{@code limit} window into it. Both are
 * "a small group of fields in one class with a length relationship" -- same detection shape,
 * different field roles -- so one strategy covers both.
 *
 * <p>Stack detection: for each {@code int} field, scans every array-index expression in the class
 * for {@code array[counter]}, {@code array[counter - k]}, {@code array[counter++]}, etc. against
 * that field. If it indexes one or more array fields at least {@link #MIN_STACK_OCCURRENCES}
 * times, the counter field gets {@code @IndexOrHigh} naming those arrays, and each such array
 * (when there is more than one) gets {@code @SameLen} naming the others.
 *
 * <p>Buffer detection is narrower and name-based: a class with {@code int pos}, {@code int limit},
 * and at least one array field is assumed to follow the {@code 0 <= pos <= limit <=
 * buffer.length} idiom (this is a very common name choice for exactly this pattern; it does not
 * attempt to discover a differently-named buffer window, and it does not attempt to model {@code
 * fillBuffer}-style helper methods' postconditions).
 */
final class StatefulInvariantStrategy implements AnnotationStrategy {

  private static final int MIN_STACK_OCCURRENCES = 3;

  @Override
  public String name() {
    return "stateful-invariant";
  }

  @Override
  public int apply(CompilationUnit cu) {
    int inserted = 0;
    for (ClassOrInterfaceDeclaration type : cu.findAll(ClassOrInterfaceDeclaration.class)) {
      inserted += annotateStackPattern(cu, type);
      inserted += annotateBufferPattern(cu, type);
    }
    return inserted;
  }

  private static int annotateStackPattern(CompilationUnit cu, ClassOrInterfaceDeclaration type) {
    Set<String> arrayFieldNames = fieldNamesOfKind(type, StatefulInvariantStrategy::isArrayField);
    Set<String> intFieldNames = fieldNamesOfKind(type, StatefulInvariantStrategy::isIntField);
    if (arrayFieldNames.isEmpty() || intFieldNames.isEmpty()) {
      return 0;
    }

    // counter field name -> (array field name -> occurrence count)
    Map<String, Map<String, Integer>> counterToArrays = new LinkedHashMap<>();
    for (ArrayAccessExpr access : type.findAll(ArrayAccessExpr.class)) {
      Optional<String> arrayName = fieldNameOf(access.getName(), arrayFieldNames);
      Optional<String> counterName = counterNameOf(access.getIndex(), intFieldNames);
      if (arrayName.isEmpty() || counterName.isEmpty()) {
        continue;
      }
      counterToArrays
          .computeIfAbsent(counterName.get(), k -> new LinkedHashMap<>())
          .merge(arrayName.get(), 1, Integer::sum);
    }

    int inserted = 0;
    for (Map.Entry<String, Map<String, Integer>> entry : counterToArrays.entrySet()) {
      int total = entry.getValue().values().stream().mapToInt(Integer::intValue).sum();
      if (total < MIN_STACK_OCCURRENCES) {
        continue;
      }
      List<String> arrays = List.copyOf(entry.getValue().keySet());
      Optional<Type> counterType = fieldType(type, entry.getKey());
      if (counterType.isPresent() && annotateIndexOrHigh(cu, counterType.get(), arrays)) {
        inserted++;
      }
      for (String array : arrays) {
        List<String> others = arrays.stream().filter(a -> !a.equals(array)).toList();
        Optional<Type> arrayType = fieldType(type, array);
        if (!others.isEmpty() && arrayType.isPresent() && annotateSameLen(cu, arrayType.get(), others)) {
          inserted++;
        }
      }
    }
    return inserted;
  }

  private static int annotateBufferPattern(CompilationUnit cu, ClassOrInterfaceDeclaration type) {
    Optional<Type> posType = fieldType(type, "pos");
    Optional<Type> limitType = fieldType(type, "limit");
    Set<String> arrayFieldNames = fieldNamesOfKind(type, StatefulInvariantStrategy::isArrayField);
    if (posType.isEmpty() || limitType.isEmpty() || arrayFieldNames.isEmpty()) {
      return 0;
    }
    List<String> arrays = List.copyOf(arrayFieldNames);
    int inserted = 0;
    if (annotateIndexOrHigh(cu, posType.get(), arrays)) {
      inserted++;
    }
    if (annotateIndexOrHigh(cu, limitType.get(), arrays)) {
      inserted++;
    }
    return inserted;
  }

  private static boolean annotateIndexOrHigh(CompilationUnit cu, Type type, List<String> arrays) {
    if (hasAnnotation(type, "IndexOrHigh") || !(type instanceof NodeWithAnnotations)) {
      return false;
    }
    ((NodeWithAnnotations<?>) type)
        .addAnnotation(StaticJavaParser.parseAnnotation("@IndexOrHigh(" + nameListLiteral(arrays) + ")"));
    cu.addImport("org.checkerframework.checker.index.qual.IndexOrHigh");
    return true;
  }

  private static boolean annotateSameLen(CompilationUnit cu, Type type, List<String> others) {
    if (hasAnnotation(type, "SameLen") || !(type instanceof NodeWithAnnotations)) {
      return false;
    }
    ((NodeWithAnnotations<?>) type)
        .addAnnotation(StaticJavaParser.parseAnnotation("@SameLen(" + nameListLiteral(others) + ")"));
    cu.addImport("org.checkerframework.checker.index.qual.SameLen");
    return true;
  }

  /** {@code ["a"]} for a single name, {@code {"a", "b"}} for several -- both valid annotation values. */
  private static String nameListLiteral(List<String> names) {
    String quoted = names.stream().map(n -> "\"" + n + "\"").reduce((a, b) -> a + ", " + b).orElse("");
    return names.size() == 1 ? quoted : "{" + quoted + "}";
  }

  private static boolean hasAnnotation(Type type, String simpleName) {
    return type.getAnnotations().stream().anyMatch(a -> a.getNameAsString().equals(simpleName));
  }

  private static Optional<String> fieldNameOf(Expression expr, Set<String> candidates) {
    if (expr instanceof NameExpr name && candidates.contains(name.getNameAsString())) {
      return Optional.of(name.getNameAsString());
    }
    if (expr instanceof FieldAccessExpr fieldAccess
        && fieldAccess.getScope() instanceof ThisExpr
        && candidates.contains(fieldAccess.getNameAsString())) {
      return Optional.of(fieldAccess.getNameAsString());
    }
    return Optional.empty();
  }

  private static Optional<String> counterNameOf(Expression expr, Set<String> candidates) {
    if (expr instanceof UnaryExpr unary) {
      return fieldNameOf(unary.getExpression(), candidates);
    }
    if (expr instanceof BinaryExpr binary
        && (binary.getOperator() == BinaryExpr.Operator.MINUS
            || binary.getOperator() == BinaryExpr.Operator.PLUS)
        && binary.getRight() instanceof IntegerLiteralExpr) {
      return fieldNameOf(binary.getLeft(), candidates);
    }
    return fieldNameOf(expr, candidates);
  }

  private static boolean isArrayField(FieldDeclaration field) {
    return field.getVariables().stream().anyMatch(v -> v.getType().isArrayType());
  }

  private static boolean isIntField(FieldDeclaration field) {
    return field.getVariables().stream()
        .anyMatch(
            v ->
                v.getType().isPrimitiveType()
                    && v.getType().asPrimitiveType().getType() == PrimitiveType.Primitive.INT);
  }

  private static Set<String> fieldNamesOfKind(
      ClassOrInterfaceDeclaration type, Predicate<FieldDeclaration> predicate) {
    Set<String> names = new LinkedHashSet<>();
    for (FieldDeclaration field : type.getFields()) {
      if (predicate.test(field)) {
        for (VariableDeclarator v : field.getVariables()) {
          names.add(v.getNameAsString());
        }
      }
    }
    return names;
  }

  private static Optional<Type> fieldType(ClassOrInterfaceDeclaration type, String fieldName) {
    for (FieldDeclaration field : type.getFields()) {
      for (VariableDeclarator v : field.getVariables()) {
        if (v.getNameAsString().equals(fieldName)) {
          return Optional.of(v.getType());
        }
      }
    }
    return Optional.empty();
  }
}
