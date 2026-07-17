package org.checkerframework.specimin.indexannotator;

import com.github.javaparser.StaticJavaParser;
import com.github.javaparser.ast.CompilationUnit;
import com.github.javaparser.ast.Node;
import com.github.javaparser.ast.body.MethodDeclaration;
import com.github.javaparser.ast.body.Parameter;
import com.github.javaparser.ast.expr.Expression;
import com.github.javaparser.ast.expr.ObjectCreationExpr;
import com.github.javaparser.ast.expr.StringLiteralExpr;
import com.github.javaparser.ast.nodeTypes.NodeWithAnnotations;
import com.github.javaparser.ast.type.ArrayType;
import java.util.List;

/**
 * Heuristic 4 (fixed/min-length inference), scoped to the {@code IntegerFieldsTypeAdapter}
 * pattern: {@code new IntegerFieldsTypeAdapter<X>("field1", "field2", ...)} allocates and passes
 * around an array whose length is exactly the number of field-name arguments given. Each such
 * anonymous subclass overrides {@code create(long[] values)}; that parameter's declared type gets
 * {@code @MinLen(N)} for that specific instantiation's N.
 *
 * <p>This only handles this one general, mechanical pattern (constructor-vararg-count -&gt;
 * array-length), not every fixed-length-array case in the codebase (e.g. {@code
 * getActualTypeArguments()} arrays whose length is fixed by an interface's generic arity would
 * need a different, type-specific rule not implemented here).
 */
final class FixedLengthStrategy implements AnnotationStrategy {

  private static final String ADAPTER_SIMPLE_NAME = "IntegerFieldsTypeAdapter";

  @Override
  public String name() {
    return "fixed-length";
  }

  @Override
  public int apply(CompilationUnit cu) {
    int inserted = 0;
    for (ObjectCreationExpr creation : cu.findAll(ObjectCreationExpr.class)) {
      if (!creation.getType().getNameAsString().equals(ADAPTER_SIMPLE_NAME)
          || creation.getAnonymousClassBody().isEmpty()) {
        continue;
      }
      int arity = countStringLiteralArgs(creation.getArguments());
      if (arity == 0) {
        continue;
      }
      for (Node member : creation.getAnonymousClassBody().get()) {
        if (!(member instanceof MethodDeclaration method)) {
          continue;
        }
        if (!method.getNameAsString().equals("create") || method.getParameters().size() != 1) {
          continue;
        }
        Parameter param = method.getParameter(0);
        if (!(param.getType() instanceof ArrayType arrayType)
            || arrayType.getAnnotationByName("MinLen").isPresent()) {
          continue;
        }
        ((NodeWithAnnotations<?>) arrayType)
            .addAnnotation(StaticJavaParser.parseAnnotation("@MinLen(" + arity + ")"));
        cu.addImport("org.checkerframework.checker.index.qual.MinLen");
        inserted++;
      }
    }
    return inserted;
  }

  private static int countStringLiteralArgs(List<Expression> args) {
    int count = 0;
    for (Expression arg : args) {
      if (arg instanceof StringLiteralExpr) {
        count++;
      }
    }
    return count;
  }
}
