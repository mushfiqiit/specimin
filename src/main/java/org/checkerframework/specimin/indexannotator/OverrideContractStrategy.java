package org.checkerframework.specimin.indexannotator;

import com.github.javaparser.StaticJavaParser;
import com.github.javaparser.ast.CompilationUnit;
import com.github.javaparser.ast.body.ClassOrInterfaceDeclaration;
import com.github.javaparser.ast.body.MethodDeclaration;
import com.github.javaparser.ast.nodeTypes.NodeWithAnnotations;
import com.github.javaparser.ast.type.ClassOrInterfaceType;
import com.github.javaparser.ast.type.Type;
import java.util.List;
import java.util.Map;
import java.util.stream.Stream;

/**
 * Heuristic 6 (override contract propagation). When a class overrides a JDK method whose Index
 * Checker / Constant Value Checker contract is known, but the override's declared return type
 * carries none of it, this copies the missing annotation onto the override.
 *
 * <p>This does not discover contracts dynamically: the Checker Framework's annotated-JDK stubs
 * that carry these contracts (e.g. {@code AbstractCollection.size()} returning
 * {@code @NonNegative int}) are not visible to JavaParser + reflection, so there is no generic way
 * to "look up" what a JDK method requires. Instead this is a small, evidence-based table of the
 * exact (supertype, method) contracts observed in gson's actual index-checker-warnings.log;
 * extend {@link #KNOWN_CONTRACTS} as new cases are found. This is the most reliable of the five
 * strategies precisely because it copies an existing contract rather than inferring behavior.
 */
final class OverrideContractStrategy implements AnnotationStrategy {

  /** One known (return-type annotation, import) contract for a zero-arg override. */
  private record Contract(String annotationSource, String importName) {}

  // supertype simple name (as it appears in `extends`/`implements`) -> method name -> contract.
  private static final Map<String, Map<String, Contract>> KNOWN_CONTRACTS =
      Map.of(
          "AbstractList",
          Map.of("size", new Contract("@NonNegative", "org.checkerframework.checker.index.qual.NonNegative")),
          "AbstractCollection",
          Map.of("size", new Contract("@NonNegative", "org.checkerframework.checker.index.qual.NonNegative")),
          "Number",
          Map.of(
              "doubleValue", new Contract("@PolyValue", "org.checkerframework.common.value.qual.PolyValue"),
              "floatValue", new Contract("@PolyValue", "org.checkerframework.common.value.qual.PolyValue"),
              "intValue", new Contract("@PolyValue", "org.checkerframework.common.value.qual.PolyValue"),
              "longValue", new Contract("@PolyValue", "org.checkerframework.common.value.qual.PolyValue")),
          "CharSequence",
          Map.of("toString", new Contract("@SameLen(\"this\")", "org.checkerframework.checker.index.qual.SameLen")),
          "WildcardType",
          Map.of(
              "getUpperBounds",
              new Contract(
                  "@ArrayLenRange(from = 1)", "org.checkerframework.common.value.qual.ArrayLenRange")));

  @Override
  public String name() {
    return "override-contract";
  }

  @Override
  public int apply(CompilationUnit cu) {
    int inserted = 0;
    for (ClassOrInterfaceDeclaration type : cu.findAll(ClassOrInterfaceDeclaration.class)) {
      Map<String, Contract> contractsForType = contractsForSupertypesOf(type);
      if (contractsForType.isEmpty()) {
        continue;
      }
      for (MethodDeclaration method : type.getMethods()) {
        if (method.getParameters().isNonEmpty() || method.getAnnotationByName("Override").isEmpty()) {
          continue;
        }
        Contract contract = contractsForType.get(method.getNameAsString());
        if (contract == null) {
          continue;
        }
        Type returnType = method.getType();
        if (alreadyAnnotated(returnType, contract)) {
          continue;
        }
        addTypeAnnotation(returnType, contract.annotationSource());
        cu.addImport(contract.importName());
        inserted++;
      }
    }
    return inserted;
  }

  /** Merges the known contracts for every supertype/interface {@code type} directly declares. */
  private static Map<String, Contract> contractsForSupertypesOf(ClassOrInterfaceDeclaration type) {
    for (ClassOrInterfaceType supertype :
        concat(type.getExtendedTypes(), type.getImplementedTypes())) {
      Map<String, Contract> contracts = KNOWN_CONTRACTS.get(supertype.getNameAsString());
      if (contracts != null) {
        return contracts;
      }
    }
    return Map.of();
  }

  private static List<ClassOrInterfaceType> concat(
      List<ClassOrInterfaceType> a, List<ClassOrInterfaceType> b) {
    return Stream.concat(a.stream(), b.stream()).toList();
  }

  private static boolean alreadyAnnotated(Type type, Contract contract) {
    String simpleName = contract.annotationSource().replaceAll("^@(\\w+).*", "$1");
    return type.getAnnotations().stream().anyMatch(a -> a.getNameAsString().equals(simpleName));
  }

  private static void addTypeAnnotation(Type type, String annotationSource) {
    if (type instanceof NodeWithAnnotations) {
      ((NodeWithAnnotations<?>) type)
          .addAnnotation(StaticJavaParser.parseAnnotation(annotationSource));
    }
  }
}
