package org.checkerframework.specimin.usagecontext;

import java.io.IOException;
import java.nio.file.Path;

/** Canonicalizes paths so diagnostic locations and parsed-file locations use the same keys. */
public final class PathNormalizer {

  private PathNormalizer() {}

  public static Path normalize(Path path) {
    try {
      return path.toAbsolutePath().normalize().toRealPath();
    } catch (IOException e) {
      return path.toAbsolutePath().normalize();
    }
  }
}
