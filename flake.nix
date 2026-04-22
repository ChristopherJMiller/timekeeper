{
  description = "timekeeper — contractor work tracker CLI (tk)";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    let
      # Exposed so `overlays.default` and the per-system outputs build the
      # same derivation from a single source of truth.
      mkTimekeeper = pkgs:
        let python = pkgs.python312; in
        python.pkgs.buildPythonApplication {
          pname = "timekeeper";
          version = "0.1.0";
          src = ./.;
          format = "pyproject";

          nativeBuildInputs = [
            pkgs.makeBinaryWrapper
            pkgs.installShellFiles
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

          # Generate shell completions via click's `_TK_COMPLETE=<shell>_source`
          # protocol. Runs after wrapping so the wrapped `tk` is on PATH.
          postInstall = ''
            installShellCompletion --cmd tk \
              --bash <(_TK_COMPLETE=bash_source $out/bin/tk) \
              --zsh  <(_TK_COMPLETE=zsh_source  $out/bin/tk) \
              --fish <(_TK_COMPLETE=fish_source $out/bin/tk)
          '';

          pythonImportsCheck = [ "worklog" "worklog.cli" ];

          # Tests spin up a temp git repo and read $HOME; run them in the
          # devShell (`pytest` / `nix flake check`) rather than during build.
          doCheck = false;

          meta = with pkgs.lib; {
            description = "Turn git commits + Claude Code sessions into weekly impact reports";
            mainProgram = "tk";
            license = licenses.mit;
            platforms = platforms.unix;
          };
        };

      # Overlay lets downstream NixOS configs do:
      #   nixpkgs.overlays = [ inputs.timekeeper.overlays.default ];
      #   environment.systemPackages = [ pkgs.timekeeper ];
      overlay = final: prev: {
        timekeeper = mkTimekeeper final;
      };
    in
    {
      overlays.default = overlay;
    }
    //
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs { inherit system; };
        python = pkgs.python312;
        timekeeper = mkTimekeeper pkgs;

        # Runtime deps + pytest. Used for the dev shell and `nix flake check`.
        devPython = python.withPackages (ps: with ps; [
          click
          openai
          keyring
          pytest
        ]);
      in
      {
        packages = {
          default = timekeeper;
          timekeeper = timekeeper;
        };

        apps.default = {
          type = "app";
          program = "${timekeeper}/bin/tk";
        };

        devShells.default = pkgs.mkShell {
          packages = [
            devPython
            pkgs.git
            pkgs.jq
          ];
          shellHook = ''
            export PYTHONPATH="$PWD/src:$PYTHONPATH"
            echo "timekeeper dev shell — pytest | python -m worklog.cli --help"
          '';
        };

        # `nix flake check` runs pytest in a sandboxed build. Git has no
        # global config in the sandbox, so tests aren't affected by the
        # user's commit.gpgsign / signing setup.
        checks.default = pkgs.stdenv.mkDerivation {
          name = "timekeeper-pytest";
          src = ./.;
          nativeBuildInputs = [ devPython pkgs.git ];
          dontBuild = true;
          doCheck = true;
          checkPhase = ''
            runHook preCheck
            export HOME=$TMPDIR
            export PYTHONPATH=$PWD/src:$PYTHONPATH
            pytest -q
            runHook postCheck
          '';
          installPhase = "mkdir -p $out && touch $out/ok";
        };
      });
}
