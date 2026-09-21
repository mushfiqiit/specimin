package org.checkerframework.specimin;

import java.io.IOException;
import org.junit.jupiter.api.Test;

/**
 * This test checks that a functional interface's single abstract method is preserved when the
 * only thing the sliced code does with that interface is create lambdas that target it (never
 * calling the abstract method itself). {@link UnusedFuncInterfaceMethodTest} checks the opposite,
 * legitimate case: a functional interface method is removed when nothing in the slice needs the
 * interface to still be functional. Specimin must not conflate "abstract method is never called
 * by name in the slice" with "abstract method is unused" -- a lambda expression whose target type
 * is this interface depends on that method structurally (to remain a valid functional interface),
 * even though the lambda never calls it explicitly; some other, unsliced code is expected to call
 * it later.
 *
 * <p>This reproduces a real bug found while slicing gson's {@code
 * com.google.gson.internal.ConstructorConstructor#newSpecialCollectionConstructor}: Specimin
 * pruned {@code com.google.gson.internal.ObjectConstructor#construct()} down to an empty
 * interface, even though {@code newSpecialCollectionConstructor} returns {@code () -> ...} lambdas
 * typed as {@code ObjectConstructor<T>}. The resulting slice does not compile: "incompatible
 * types: ObjectConstructor is not a functional interface / no abstract method found in interface
 * ObjectConstructor".
 */
public class FuncInterfaceMethodUsedByLambdaOnlyTest {
  @Test
  public void runTest() throws IOException {
    SpeciminTestExecutor.runTestWithoutJarPaths(
        "funcinterfacemethodusedbylambdaonly",
        new String[] {"com/example/Simple.java"},
        new String[] {"com.example.Simple#makeConstructor()"});
  }
}
