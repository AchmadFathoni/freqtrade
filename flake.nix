{
  description = "Freqtrade crypto trading bot";
  inputs = {
    # nixpkgs.url = "nixpkgs/nixos-25.05";
    nixpkgs.url = "git+file:///home/toni/Documents/source/nixpkgs";
  };
  outputs = {
    nixpkgs,
    ...
  }:
    let
      system = "x86_64-linux";
      pkgs = nixpkgs.legacyPackages.${system};
      buildInputs = [
        (pkgs.python3.withPackages (python-pkgs: [
          python-pkgs.pip
          python-pkgs.virtualenv
        ]))

        # Matplotlib run dependency
        pkgs.glib
        pkgs.zlib
        pkgs.zstd
        pkgs.libGL
        pkgs.fontconfig
        pkgs.libxkbcommon
        pkgs.freetype
        pkgs.dbus
        pkgs.libX11
      ];
    in {
      devShells.${system} = {
        default = pkgs.mkShell{
          inherit buildInputs;
          shellHook = ''
            export LD_LIBRARY_PATH="${pkgs.stdenv.cc.cc.lib}/lib:${pkgs.lib.makeLibraryPath buildInputs}"
            source .venv/bin/activate
          '';
        };

        setupShell = pkgs.mkShell {
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
      };
    };
}
