{
  description = "Zeus - Home Assistant custom integration development environment";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
    pre-commit-hooks = {
      url = "github:cachix/git-hooks.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = {
    self,
    nixpkgs,
    flake-utils,
    pre-commit-hooks,
  }:
    flake-utils.lib.eachDefaultSystem (
      system: let
        pkgs = nixpkgs.legacyPackages.${system};

        python = pkgs.python313;

        # Python with base packages from nixpkgs. HA-specific deps are
        # installed into a local venv via pip (homeassistant and its test
        # helpers are not packaged in nixpkgs as Python libraries).
        pythonEnv = python.withPackages (ps:
          with ps; [
            pip
            setuptools
            wheel
            virtualenv
          ]);
      in {
        checks = {
          pre-commit-check = pre-commit-hooks.lib.${system}.run {
            src = ./.;
            hooks = {
              ruff-lint = {
                enable = true;
                name = "ruff-lint";
                entry = "${pkgs.ruff}/bin/ruff check --fix";
                types = ["python"];
              };
              ruff-format = {
                enable = true;
                name = "ruff-format";
                entry = "${pkgs.ruff}/bin/ruff format";
                types = ["python"];
              };
              check-json = {
                enable = true;
                name = "check-json";
                entry = "${pkgs.python313Packages.pre-commit-hooks}/bin/check-json";
                types = ["json"];
              };
              check-yaml = {
                enable = true;
                name = "check-yaml";
                entry = "${pkgs.python313Packages.pre-commit-hooks}/bin/check-yaml";
                types = ["yaml"];
              };
            };
          };
        };

        # `nix run` – start a local Home Assistant instance for manual testing.
        apps.default = let
          run-hass = pkgs.writeShellApplication {
            name = "run-hass";
            runtimeInputs = [];
            text = ''
              # Activate the venv
              if [ ! -d .venv ]; then
                echo "No .venv found. Enter the dev shell first (nix develop) to set it up."
                exit 1
              fi
              # shellcheck source=/dev/null
              source .venv/bin/activate

              # Create config dir if not present
              if [ ! -d "''${PWD}/config" ]; then
                mkdir -p "''${PWD}/config"
                hass --config "''${PWD}/config" --script ensure_config
              fi

              # Let HA discover custom_components without a symlink
              export PYTHONPATH="''${PYTHONPATH:+''${PYTHONPATH}:}''${PWD}"

              # Start Home Assistant
              hass --config "''${PWD}/config" --debug
            '';
          };
        in {
          type = "app";
          program = "${run-hass}/bin/run-hass";
        };

        devShells.default = pkgs.mkShell {
          name = "zeus-ha-integration";

          buildInputs = [
            pythonEnv
            pkgs.ruff
            pkgs.pre-commit
            pkgs.mypy
          ];

          # Required for building some HA dependencies with native extensions.
          nativeBuildInputs = with pkgs; [
            pkg-config
            gcc
            libffi
            openssl
          ];

          env = {
            # Ensure pip installs into the local venv, not the nix store.
            PIP_PREFIX = "";
          };

          shellHook = ''
            ${self.checks.${system}.pre-commit-check.shellHook}

            # Create and activate a local venv for HA python dependencies.
            if [ ! -d .venv ]; then
              echo "Creating Python virtual environment..."
              python -m venv .venv
            fi
            source .venv/bin/activate

            # Install HA dev dependencies into the venv if not already present.
            if ! python -c "import homeassistant" 2>/dev/null; then
              echo "Installing Home Assistant and dev dependencies into .venv..."
              pip install --quiet --upgrade pip
              pip install --quiet -r requirements_dev.txt
            fi

            echo ""
            echo "Zeus - Home Assistant Integration Development Environment"
            echo "========================================================="
            echo "Python:  $(python --version)"
            echo "Ruff:    $(ruff --version)"
            echo "Pytest:  $(pytest --version 2>/dev/null | head -1)"
            echo ""
            echo "Commands:"
            echo "  nix run               - Start Home Assistant dev instance"
            echo "  ruff check .          - Lint the codebase"
            echo "  ruff format .         - Format the codebase"
            echo "  pytest                - Run tests"
            echo "  pytest --cov          - Run tests with coverage"
            echo ""
          '';
        };
      }
    );
}
