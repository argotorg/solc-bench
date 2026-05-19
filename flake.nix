{
  description = "Solidity compiler benchmark tool";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-25.11";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system: let
      pkgs = import nixpkgs { inherit system; };
      pyproject = builtins.fromTOML (builtins.readFile ./pyproject.toml);
      versionLine = pkgs.lib.lists.head (
        pkgs.lib.lists.filter (line: pkgs.lib.strings.hasPrefix "VERSION = " line)
          (pkgs.lib.strings.splitString "\n"
            (builtins.readFile ./src/solc_bench/__init__.py))
      );
      version = builtins.head (builtins.match "VERSION = \"([^\"]+)\"" versionLine);
      solc-bench = pkgs.python3Packages.buildPythonApplication {
        pname = pyproject.project.name;
        inherit version;
        pyproject = true;
        src = self;
        build-system = with pkgs.python3Packages; [ hatchling ];
        dependencies = with pkgs.python3Packages; [ packaging requests tomlkit ];
        doCheck = false;
        # gnuplot powers the inline terminal scatter plot drawn by `compare`.
        nativeBuildInputs = [ pkgs.makeWrapper ];
        makeWrapperArgs = [
          "--prefix" "PATH" ":" (pkgs.lib.makeBinPath [ pkgs.gnuplot ])
        ];
      };
    in {
      packages = {
        default = solc-bench;
        solc-bench = solc-bench;
      };

      devShells.default = pkgs.mkShell {
        inputsFrom = [ solc-bench ];
        packages = with pkgs; [
          foundry
          gnuplot
          perf
          python3Packages.seaborn
        ];
      };
    });
}
