{
  description = "timekeeper — contractor work tracker CLI (tk)";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs { inherit system; };
        python = pkgs.python311;

        # -------------------------------------------------------------------
        # Runtime optimizations:
        #
        # 1. The `openai` and `keyring` packages are imported LAZILY inside
        #    `worklog.summarize` and `worklog.auth`, so the hot-path commands
        #    (`tk start`, `tk stop --no-summary`, `tk status`, `tk list`)
        #    never pay their import cost (~200–400 ms combined).
        #
        # 2. We use `makeBinaryWrapper` instead of the default shell wrapper
        #    around the entry point — saves ~5–15 ms per invocation on Linux
        #    and avoids a bash exec.
        #
        # 3. `buildPythonApplication` precompiles `.pyc` files at install
        #    time, so the first run doesn't pay compilation cost.
        #
        # 4. Only runtime deps (no dev tooling) end up in the closure of the
        #    `tk` binary; pytest lives in the devShell only.
        # -------------------------------------------------------------------

        timekeeper = python.pkgs.buildPythonApplication {
          pname = "timekeeper";
          version = "0.1.0";
          src = ./.;
          format = "pyproject";

          nativeBuildInputs = [
            pkgs.makeBinaryWrapper
          ] ++ (with python.pkgs; [ setuptools wheel ]);

          propagatedBuildInputs = with python.pkgs; [
            click
            openai
            keyring
          ];

          # git + jq are invoked by `tk` (for evidence collection) and by the
          # Claude Code Stop hook. Pinning them onto PATH means the CLI works
          # on any system without relying on the user's environment.
          makeWrapperArgs = [
            "--prefix PATH : ${pkgs.lib.makeBinPath [ pkgs.git pkgs.jq ]}"
          ];

          pythonImportsCheck = [ "worklog" "worklog.cli" ];

          # Tests touch $HOME and spin up a temp git repo; run them in the
          # devShell (`pytest`) rather than as part of the package build.
          doCheck = false;

          meta = with pkgs.lib; {
            description = "Turn git commits + Claude Code sessions into weekly impact reports";
            mainProgram = "tk";
            license = licenses.mit;
            platforms = platforms.unix;
          };
        };

        devEnv = python.withPackages (ps: with ps; [
          click
          openai
          keyring
          pytest
        ]);
      in {
        packages = {
          default = timekeeper;
          timekeeper = timekeeper;
        };

        apps.default = {
          type = "app";
          program = "${timekeeper}/bin/tk";
        };

        devShells.default = pkgs.mkShell {
          packages = [ devEnv pkgs.git pkgs.jq ];
          shellHook = ''
            export PYTHONPATH="$PWD/src:$PYTHONPATH"
            echo "timekeeper dev shell — pytest | python -m worklog.cli --help"
          '';
        };
      });
}
