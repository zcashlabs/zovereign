{
  description = "Snapshot-bootstrapped Zakura and lightwalletd, without Docker";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/2074cc672349f14240fb7ea90d509e0b643e9dd4";
  # 26.11 dropped Intel Macs; keep that target on the maintained 26.05 branch.
  inputs.nixpkgsIntelDarwin.url = "github:NixOS/nixpkgs/f4244ef33645badc8157562b5e80748929b396c2";

  outputs = { self, nixpkgs, nixpkgsIntelDarwin }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" "x86_64-darwin" "aarch64-darwin" ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
    in {
      packages = forAllSystems (system:
        let
          intelDarwin = system == "x86_64-darwin";
          pkgs = import (if intelDarwin then nixpkgsIntelDarwin else nixpkgs) { inherit system; };
          rustPlatform = if intelDarwin then pkgs.makeRustPlatform {
            inherit (pkgs.rustPackages_1_98) cargo rustc;
          } else pkgs.rustPlatform;
          zakura = pkgs.callPackage ./nix/zakura.nix { inherit rustPlatform; };
          lightwalletd = pkgs.callPackage ./nix/lightwalletd.nix { };
          runtime = [ pkgs.python3 pkgs.curl pkgs.zstd pkgs.grpcurl zakura lightwalletd ];
          source = pkgs.runCommand "zovereign-runtime" { } ''
            mkdir -p "$out"
            cp ${./docker/stack.py} "$out/stack.py"
            cp ${./nix/native.py} "$out/native.py"
          '';
          zovereign = pkgs.writeShellApplication {
            name = "zovereign";
            runtimeInputs = runtime;
            text = ''
              export PYTHONUNBUFFERED=1
              export SSL_CERT_FILE="''${SSL_CERT_FILE:-${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt}"
              export CURL_CA_BUNDLE="''${CURL_CA_BUNDLE:-$SSL_CERT_FILE}"
              exec python3 ${source}/native.py "$@"
            '';
          };
        in {
          inherit zakura lightwalletd zovereign;
          default = zovereign;
          test = pkgs.writeShellApplication {
            name = "zovereign-test";
            runtimeInputs = runtime;
            text = ''
              export PYTHONUNBUFFERED=1
              export PYTHONDONTWRITEBYTECODE=1
              export STACK_MODULE=${source}/stack.py
              export NATIVE_MODULE=${source}/native.py
              export ZOVEREIGN_BIN=${zovereign}/bin/zovereign
              exec python3 ${./tests}/test_native.py "$@"
            '';
          };
        });

      apps = forAllSystems (system: {
        default = {
          type = "app";
          meta.description = "Run the native snapshot-bootstrapped Zakura and lightwalletd stack";
          program = "${self.packages.${system}.default}/bin/zovereign";
        };
        test = {
          type = "app";
          meta.description = "Run local fixtures and native daemon regressions";
          program = "${self.packages.${system}.test}/bin/zovereign-test";
        };
      });

      checks = forAllSystems (system: {
        runtime = self.packages.${system}.default;
      });
    };
}
