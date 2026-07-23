package org.checkerframework.specimin.indexannotator;

import com.github.javaparser.StaticJavaParser;
import com.github.javaparser.ast.CompilationUnit;
import com.github.javaparser.ast.body.MethodDeclaration;
import com.github.javaparser.ast.body.Parameter;
import com.github.javaparser.ast.expr.Expression;
import com.github.javaparser.ast.expr.MethodCallExpr;
import com.github.javaparser.ast.expr.NameExpr;
import com.github.javaparser.ast.expr.ObjectCreationExpr;
import com.github.javaparser.ast.nodeTypes.NodeWithAnnotations;
import com.github.javaparser.ast.type.PrimitiveType;
import com.github.javaparser.ast.type.Type;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;

/**
 * Heuristics 3 and 5, merged: a method parameter that flows unmodified into a JDK call which
 * itself requires an Index Checker qualifier. Real gson data shows these are the same mechanism
 * applied to two parameter shapes:
 *
 * <ul>
 *   <li>a single index/capacity parameter passed straight to {@code List.get/set/add/remove},
 *       {@code String.charAt}, or an {@code ArrayList}/{@code HashMap}/{@code StringBuilder}
 *       capacity constructor -&gt; add {@code @NonNegative};
 *   <li>an (offset, length) parameter pair passed straight to {@code append}/{@code new String}
 *       alongside a sequence parameter (or field) of the same method -&gt; add {@code @NonNegative}
 *       to both, plus {@code @LTLengthOf} on the offset relative to the sequence and the length.
 * </ul>
 *
 * <p>"Unmodified" is checked syntactically (the argument must be a bare reference to the
 * parameter, not an arithmetic expression on it) -- this only catches direct pass-through, not
 * cases where the method transforms the value first, which is intentional: propagating a
 * precondition across a transformation is not sound without knowing the transformation.
 */
final class DelegatedPreconditionStrategy implements AnnotationStrategy {

  // method name -> the 0-based argument position that must be @NonNegative.
  private static final Map<String, Integer> SINGLE_INDEX_METHODS =
      Map.of("get", 0, "set", 0, "add", 0, "remove", 0, "charAt", 0, "setIndex", 0);

  private static final Set<String> CAPACITY_CONSTRUCTORS =
      Set.of("ArrayList", "HashMap", "StringBuilder", "StringBuffer");

  private static final Set<String> OFFSET_LENGTH_APPEND_METHODS = Set.of("append", "write");

  private static final Set<String> INDEX_QUALIFIER_NAMES =
      Set.of("NonNegative", "Positive", "IndexFor", "LTLengthOf", "IndexOrHigh");

  @Override
  public String name() {
    return "delegated-precondition";
  }

  @Override
  public int apply(CompilationUnit cu) {
    int inserted = 0;
    for (MethodDeclaration method : cu.findAll(MethodDeclaration.class)) {
      for (Parameter param : method.getParameters()) {
        if (!isIntType(param.getType()) || hasIndexQualifier(param.getType())) {
          continue;
        }
        if (isPassedAsSingleIndex(method, param) && annotate(cu, param.getType(), "@NonNegative")) {
          inserted++;
        }
      }
      inserted += annotateOffsetLengthPairs(cu, method);
    }
    return inserted;
  }

  /** True if {@code param} is ever passed, unmodified, as a known single-index/capacity argument. */
  private static boolean isPassedAsSingleIndex(MethodDeclaration method, Parameter param) {
    for (MethodCallExpr call : method.findAll(MethodCallExpr.class)) {
      Integer position = SINGLE_INDEX_METHODS.get(call.getNameAsString());
      if (position != null && argIsBareParam(call.getArguments(), position, param)) {
        return true;
      }
    }
    for (ObjectCreationExpr creation : method.findAll(ObjectCreationExpr.class)) {
      if (CAPACITY_CONSTRUCTORS.contains(creation.getType().getNameAsString())
          && creation.getArguments().size() == 1
          && argIsBareParam(creation.getArguments(), 0, param)) {
        return true;
      }
    }
    return false;
  }

  /**
   * Finds {@code append(sequence, offset, length)}-shaped calls (or {@code new String(...)}) where
   * {@code offset} and {@code length} are bare parameters of {@code method} and {@code sequence} is
   * a parameter or field reference, and annotates the offset/length parameters accordingly.
   */
  private static int annotateOffsetLengthPairs(CompilationUnit cu, MethodDeclaration method) {
    int inserted = 0;
    for (MethodCallExpr call : method.findAll(MethodCallExpr.class)) {
      if (OFFSET_LENGTH_APPEND_METHODS.contains(call.getNameAsString())) {
        inserted += tryAnnotateTriple(cu, method, call.getArguments());
      }
    }
    for (ObjectCreationExpr creation : method.findAll(ObjectCreationExpr.class)) {
      if (creation.getType().getNameAsString().equals("String")) {
        inserted += tryAnnotateTriple(cu, method, creation.getArguments());
      }
    }
    return inserted;
  }

  private static int tryAnnotateTriple(
      CompilationUnit cu, MethodDeclaration method, List<Expression> args) {
    if (args.size() < 3) {
      return 0;
    }
    Optional<Parameter> offsetParam = bareParamOf(method, args.get(1));
    Optional<Parameter> lengthParam = bareParamOf(method, args.get(2));
    Optional<String> sequenceRef = sequenceReference(method, args.get(0));
    if (offsetParam.isEmpty() || lengthParam.isEmpty() || sequenceRef.isEmpty()) {
      return 0;
    }
    int inserted = 0;
    Type offsetType = offsetParam.get().getType();
    if (!hasIndexQualifier(offsetType)) {
      annotate(cu, offsetType, "@NonNegative");
      annotate(
          cu,
          offsetType,
          "@LTLengthOf(value = \""
              + sequenceRef.get()
              + "\", offset = \""
              + lengthParam.get().getNameAsString()
              + " - 1\")");
      cu.addImport("org.checkerframework.checker.index.qual.LTLengthOf");
      inserted++;
    }
    Type lengthType = lengthParam.get().getType();
    if (!hasIndexQualifier(lengthType)) {
      annotate(cu, lengthType, "@NonNegative");
      inserted++;
    }
    return inserted;
  }

  /** If {@code arg} is a bare reference to one of {@code method}'s own parameters, returns it. */
  private static Optional<Parameter> bareParamOf(MethodDeclaration method, Expression arg) {
    if (!(arg instanceof NameExpr name)) {
      return Optional.empty();
    }
    return method.getParameters().stream()
        .filter(p -> p.getNameAsString().equals(name.getNameAsString()))
        .findFirst();
  }

  /** If {@code arg} refers to a parameter or a bare {@code this.field}-style name, its text. */
  private static Optional<String> sequenceReference(MethodDeclaration method, Expression arg) {
    if (!(arg instanceof NameExpr name)) {
      return Optional.empty();
    }
    boolean isParam =
        method.getParameters().stream().anyMatch(p -> p.getNameAsString().equals(name.getNameAsString()));
    return isParam ? Optional.of(name.getNameAsString()) : Optional.empty();
  }

  private static boolean argIsBareParam(List<Expression> args, int position, Parameter param) {
    if (position >= args.size() || !(args.get(position) instanceof NameExpr name)) {
      return false;
    }
    return name.getNameAsString().equals(param.getNameAsString());
  }

  private static boolean isIntType(Type type) {
    return type.isPrimitiveType()
        && type.asPrimitiveType().getType() == PrimitiveType.Primitive.INT;
  }

  private static boolean hasIndexQualifier(Type type) {
    return type.getAnnotations().stream()
        .anyMatch(a -> INDEX_QUALIFIER_NAMES.contains(a.getNameAsString()));
  }

  private static boolean annotate(CompilationUnit cu, Type type, String annotationSource) {
    if (!(type instanceof NodeWithAnnotations)) {
      return false;
    }
    ((NodeWithAnnotations<?>) type).addAnnotation(StaticJavaParser.parseAnnotation(annotationSource));
    if (annotationSource.startsWith("@NonNegative")) {
      cu.addImport("org.checkerframework.checker.index.qual.NonNegative");
    }
    return true;
  }
}
