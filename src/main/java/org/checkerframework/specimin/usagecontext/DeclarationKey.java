package org.checkerframework.specimin.usagecontext;

import com.github.javaparser.resolution.declarations.ResolvedFieldDeclaration;
import com.github.javaparser.resolution.declarations.ResolvedValueDeclaration;
import java.util.Objects;
import java.util.Optional;
import org.checkerframework.checker.nullness.qual.Nullable;

/**
 * A stable string identity for a resolved declaration, usable across separately-parsed
 * CompilationUnits (JavaParser's resolved declarations from different parse sessions are not
 * directly comparable).
 *
 * <p>Scoped to fields for now, since that covers {@code FixSpuriousNullable.py}'s original use case
 * and keeps the rewriter's node-location logic unambiguous. Parameters/locals/return types are a
 * natural extension point: add a case here plus a matching lookup in {@link AstAnnotationRewriter}.
 */
public final class DeclarationKey {

  public final String qualifiedOwner;
  public final String memberName;

  private DeclarationKey(String qualifiedOwner, String memberName) {
    this.qualifiedOwner = qualifiedOwner;
    this.memberName = memberName;
  }

  public static Optional<DeclarationKey> of(ResolvedValueDeclaration decl) {
    if (decl.isField()) {
      ResolvedFieldDeclaration field = decl.asField();
      String owner = field.declaringType().getQualifiedName();
      return Optional.of(new DeclarationKey(owner, field.getName()));
    }
    return Optional.empty();
  }

  @Override
  public String toString() {
    return qualifiedOwner + "#" + memberName;
  }

  @Override
  public boolean equals(@Nullable Object o) {
    if (!(o instanceof DeclarationKey other)) {
      return false;
    }
    return qualifiedOwner.equals(other.qualifiedOwner) && memberName.equals(other.memberName);
  }

  @Override
  public int hashCode() {
    return Objects.hash(qualifiedOwner, memberName);
  }
}
