{ lib, rustPlatform, fetchurl, stdenv }:

rustPlatform.buildRustPackage {
  pname = "zakura";
  version = "1.8.7";

  src = fetchurl {
    name = "zakura-1.8.7.tar.gz";
    url = "https://codeload.github.com/zakura-core/zakura/tar.gz/1b2418c151a184f2a83cc161ddbbd6c017b5675b";
    hash = "sha256-XNC9NebtBaFUAiMHVK/ICf8clJQ2xHqeR9eydRcHBY0=";
  };
  cargoHash = "sha256-iLdiayHcfDBYhemZxMEQ4Uj8pBv9MMHpkd3Fb8WZYvQ=";

  cargoBuildFlags = [ "--package" "zakura" "--bin" "zakurad" ];
  buildFeatures = lib.optional stdenv.hostPlatform.isx86_64 "portable";
  # Upstream's full suite exercises the blockchain/network. The packaged runtime
  # is checked with local snapshots and real-binary smoke tests by .#test.
  doCheck = false;

  meta = {
    description = "Zakura Zcash full node";
    homepage = "https://github.com/zakura-core/zakura";
    license = with lib.licenses; [ mit asl20 ];
    mainProgram = "zakurad";
    platforms = [ "x86_64-linux" "aarch64-linux" "x86_64-darwin" "aarch64-darwin" ];
  };
}
