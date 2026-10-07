{
  self,
  pkgs,
  home-manager,
}:

let
  inherit (pkgs) lib;
  inherit (pkgs.stdenv.hostPlatform) isDarwin isLinux system;
  debrief = self.packages.${system}.debrief;
  homeDirectory = if isDarwin then "/Users/alice" else "/home/alice";

  home =
    modules:
    home-manager.lib.homeManagerConfiguration {
      inherit pkgs;
      modules = [
        self.homeModules.default
        {
          home = {
            username = "alice";
            inherit homeDirectory;
            stateVersion = "26.05";
          };
          programs.debrief = {
            enable = true;
            package = debrief;
          };
        }
      ]
      ++ modules;
    };

  full = home [
    {
      programs.debrief = {
        agents = [
          "claude"
          "codex"
          "devin"
        ];
        claudeSettings = true;
        settings = {
          residency.allow_hosts = [
            "git.example.com"
            "*.example.com"
          ];
          viewer.port = 7400;
          experimental.compaction = false;
        };
        service.enable = true;
      };
    }
  ];
  codexOnly = home [ { programs.debrief.agents = [ "codex" ]; } ];
  movedClaude = home [
    {
      programs.claude-code.configDir = "${homeDirectory}/.config/claude";
      programs.debrief.agents = [ "claude" ];
    }
  ];
  misconfigured = home [
    {
      programs.debrief = {
        agents = [ "codex" ];
        claudeSettings = true;
      };
    }
  ];

  src = lib.fileset.toSource {
    root = ../.;
    fileset = lib.fileset.unions [
      ../build.py
      ../src
      ../tests
    ];
  };

  # Runs one activation step from a configuration, as a shell function.
  step = hm: name: ''
    ${name}() {
    ${hm.config.home.activation.${name}.data}
    }
  '';
in
{
  package = debrief;

  # The Python suite (the browser tests skip themselves without Playwright).
  tests = pkgs.stdenvNoCC.mkDerivation {
    name = "debrief-tests";
    inherit src;
    nativeBuildInputs = [
      pkgs.python3
      pkgs.git
      pkgs.openssl
    ];
    __darwinAllowLocalNetworking = true;
    dontConfigure = true;
    buildPhase = ''
      export HOME="$TMPDIR/home"
      mkdir -p "$HOME"
      PYTHONPATH="$PWD/src" python3 -m unittest discover -s tests -t . -v
    '';
    installPhase = "touch $out";
  };

  # The module renders the block in Nix; it must match what Debrief renders.
  block-text = pkgs.runCommand "debrief-block-text" { } ''
    diff -u ${debrief}/share/debrief/agent/block.md \
      ${pkgs.writeText "module-block.md" full.config.programs.debrief.blockText}
    touch $out
  '';

  # claudeSettingsFragment must match what `--claude-settings` writes.
  claude-settings-fragment =
    pkgs.runCommand "debrief-claude-settings-fragment" { nativeBuildInputs = [ pkgs.python3 ]; }
      ''
        export HOME="$TMPDIR/home"
        mkdir -p "$HOME"
        ${debrief}/bin/debrief install --managed --harness claude --claude-settings
        python3 - ${pkgs.writeText "fragment.json" (builtins.toJSON full.config.programs.debrief.claudeSettingsFragment)} <<'EOF'
        import json, os, sys
        home = os.environ["HOME"]
        with open(os.path.join(home, ".claude", "settings.json")) as f:
            written = json.load(f)
        with open(sys.argv[1]) as f:
            fragment = json.loads(f.read().replace("${homeDirectory}", home))
        assert written == fragment, (written, fragment)
        EOF
        touch $out
      '';

  # Option wiring that needs no build: a moved Claude Code directory, the
  # claudeSettings assertion, and the order of the activation steps.
  module-eval =
    let
      files = hm: lib.attrNames hm.config.home.file;
      # The order Home Manager runs the activation steps in.
      steps = map (s: s.name) (home-manager.lib.hm.dag.topoSort full.config.home.activation).result;
      at = name: lib.lists.findFirstIndex (s: s == name) (throw "no activation step ${name}") steps;
    in
    assert at "debriefMigrate" < at "checkLinkTargets";
    assert at "linkGeneration" < at "debrief";
    assert lib.elem "${homeDirectory}/.claude/skills/ai-session" (files full);
    assert !lib.elem "${homeDirectory}/.claude/skills/ai-session" (files codexOnly);
    assert lib.elem ".agents/skills/ai-session" (files codexOnly);
    assert lib.elem "${homeDirectory}/.config/claude/skills/ai-session" (files movedClaude);
    assert lib.hasInfix "export CLAUDE_CONFIG_DIR=" movedClaude.config.home.activation.debrief.data;
    assert !lib.hasInfix "CLAUDE_CONFIG_DIR" full.config.home.activation.debrief.data;
    # Home Manager throws on a failed assertion as soon as the configuration is read.
    assert !(builtins.tryEval misconfigured.config.home.file).success;
    assert (pkgs.extend self.overlays.default).debrief.drvPath == debrief.drvPath;
    pkgs.runCommand "debrief-module-eval" { } "touch $out";

  # Home Manager's own collision check and linking, with this module's steps
  # around them, over a home that a manual `debrief install` set up before;
  # then a switch to a configuration with Claude Code dropped.
  home-manager =
    pkgs.runCommand "debrief-home-manager"
      {
        nativeBuildInputs = [
          pkgs.gettext
          pkgs.python3
        ];
      }
      ''
        set -euo pipefail
        export HOME="$TMPDIR/home" USER=alice
        mkdir -p "$HOME/.claude"
        cd "$HOME"
        ${full.config.lib.bash.initHomeManagerLib}
        VERBOSE_ARG=""
        ${step full "debriefMigrate"}
        ${step full "checkLinkTargets"}
        ${step full "linkGeneration"}
        ${step full "debrief"}

        fail() { echo "FAIL: $*" >&2; exit 1; }
        inStore() { [[ $(readlink -f "$1") == ${builtins.storeDir}/* ]] || fail "$1 is not linked into the store"; }
        has() { grep -qF -- "$2" "$1" || fail "$1 lacks: $2"; }
        lacks() { if grep -qF -- "$2" "$1"; then fail "$1 still has: $2"; fi; }

        printf '# My rules\n' > .claude/CLAUDE.md
        ${debrief}/bin/debrief install --harness claude,codex --claude-settings > /dev/null
        [[ -f .local/share/debrief/debrief.pyz && -d .agents/skills/ai-session ]] || fail "manual install"

        newGenPath=${full.activationPackage}
        debriefMigrate
        [[ ! -e .local/share/debrief/debrief.pyz && ! -e .local/bin/debrief ]] || fail "manual install left behind"
        checkLinkTargets
        linkGeneration
        debrief

        for f in .local/bin/debrief-session .agents/skills/ai-session .agents/skills/ai-session-closeout \
                 .claude/skills/ai-session .claude/skills/ai-session-closeout .config/debrief/config; do
          inStore "$f"
        done
        .local/bin/debrief-session --help > /dev/null
        [[ $(head -n1 .claude/CLAUDE.md) == "# My rules" ]] || fail "CLAUDE.md lost the user's text"
        has .claude/CLAUDE.md "<!-- debrief:begin v"
        has .claude/CLAUDE.md '`bin/session` below means `~/.local/bin/debrief-session`'
        has .codex/AGENTS.md "<!-- debrief:begin v"
        [[ ! -e .config/devin/AGENTS.md ]] || fail "Devin should read the block from CLAUDE.md"
        has .claude/settings.json "$HOME/.local/bin/debrief-session context"
        has .config/debrief/config "allow_hosts = git.example.com, *.example.com"
        has .config/debrief/config "port = 7400"
        ${lib.optionalString isLinux ''
          has .config/systemd/user/debrief.service "ExecStart=${builtins.storeDir}/"
          has .config/systemd/user/debrief.service "WantedBy=default.target"
        ''}
        ${lib.optionalString isDarwin ''
          has "$newGenPath/LaunchAgents/org.nix-community.home.debrief.plist" "debrief-serve"
        ''}
        before=$(cat .claude/CLAUDE.md .codex/AGENTS.md .claude/settings.json | sha256sum)
        out=$(debrief 2>&1)
        [[ -z $out ]] || fail "a second activation reported: $out"
        [[ $(cat .claude/CLAUDE.md .codex/AGENTS.md .claude/settings.json | sha256sum) == "$before" ]] \
          || fail "a second activation changed files"

        # Switch to a configuration without Claude Code.
        unset -f debrief checkLinkTargets linkGeneration
        ${step codexOnly "checkLinkTargets"}
        ${step codexOnly "linkGeneration"}
        ${step codexOnly "debrief"}
        oldGenPath=$newGenPath newGenPath=${codexOnly.activationPackage}
        checkLinkTargets
        linkGeneration
        debrief
        [[ $(cat .claude/CLAUDE.md) == "# My rules" ]] || fail "CLAUDE.md keeps more than the user's text"
        lacks .claude/settings.json debrief-session
        [[ ! -e .claude/skills/ai-session && ! -e .config/debrief/config ]] || fail "old links remain"
        has .codex/AGENTS.md "<!-- debrief:begin v"
        inStore .agents/skills/ai-session
        touch $out
      '';
}
