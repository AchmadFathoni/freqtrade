# Run
#
#   nix develop .#setupShell
#
# to install packages via pip initially (and stay in shell).
# Deletes your `.venv`!
#
# Run
#
#   nix develop
#
# to get a development shell afterwards.

{
  description = "Freqtrade crypto trading bot";
  inputs = {
    nixpkgs.url = "nixpkgs/nixos-24.11";
    # or for unstable
    # nixpkgs.url = "nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };
  outputs = {
    nixpkgs,
    flake-utils,
    ...
  }:
    flake-utils.lib.eachDefaultSystem (system: let
      pkgs = nixpkgs.legacyPackages.${system};
      buildInputs = [
        (pkgs.python3.withPackages (python-pkgs: [
          python-pkgs.pip
          python-pkgs.virtualenv
          python-pkgs.spyder
        ]))
        pkgs.zlib
        (pkgs.ta-lib.overrideAttrs (finalAttrs: previousAttrs: {
          prePatch = ''sed -i.bak "s|0.00000001|0.000000000000000001 |g" src/ta_func/ta_utility.h'';
        }))
        # Matplotlib run dependency
        pkgs.glib
        pkgs.zlib
        pkgs.zstd
        pkgs.libGL
        pkgs.fontconfig
        pkgs.libxkbcommon
        pkgs.freetype
        pkgs.dbus
        pkgs.xcb-util-cursor
        pkgs.xorg.libX11
        pkgs.xorg.libxcb
        pkgs.xorg.xcbutilwm
        pkgs.xorg.xcbutilimage
        pkgs.xorg.xcbutilkeysyms
        pkgs.xorg.xcbutilrenderutil
      ];
    in {
      devShells.default = pkgs.mkShell {
        inherit buildInputs;
        shellHook = ''
          export LD_LIBRARY_PATH=${pkgs.stdenv.cc.cc.lib}/lib/
          export LD_LIBRARY_PATH="${pkgs.lib.makeLibraryPath buildInputs}:$LD_LIBRARY_PATH"
          source .venv/bin/activate
        '';
      };
      devShells.setupShell = pkgs.mkShell {
        inherit buildInputs;
        #TODO: make requirements-tony.txt
        shellHook = ''
          export LD_LIBRARY_PATH=${pkgs.stdenv.cc.cc.lib}/lib/
          rm -rf .venv
          virtualenv --no-setuptools .venv
          source .venv/bin/activate
          pip install -r requirements-dev.txt
          pip install -r requirements-tony.txt
          pip install .[jupyter]
          pip install -e .
        '';
      };
    });
}
