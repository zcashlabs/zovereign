{ lib, buildGo127Module, fetchurl }:

buildGo127Module {
  pname = "lightwalletd";
  version = "0.6.0";

  src = fetchurl {
    name = "lightwalletd-0.6.0.tar.gz";
    url = "https://codeload.github.com/zcash/lightwalletd/tar.gz/e7436d2adb9ab5828eb8a27de534e16218841fc2";
    sha256 = "96e6e788a4edd618e1b17cc2de8c5ce614a1fea498e7889e86b87d354eb89f7d";
  };
  vendorHash = "sha256-LyWbitsjw6YXxGnxd53SCkD6AL5p8YY6hLQJxgVIIRw=";
  subPackages = [ "." ];
  env.CGO_ENABLED = "0";
  ldflags = [
    "-s" "-w"
    "-X github.com/zcash/lightwalletd/common.Version=v0.6.0"
    "-X github.com/zcash/lightwalletd/common.GitCommit=e7436d2adb9ab5828eb8a27de534e16218841fc2"
  ];
  doCheck = false;

  meta = {
    description = "Zcash compact-block gRPC server";
    homepage = "https://github.com/zcash/lightwalletd";
    license = lib.licenses.mit;
    mainProgram = "lightwalletd";
    platforms = [ "x86_64-linux" "aarch64-linux" "x86_64-darwin" "aarch64-darwin" ];
  };
}
