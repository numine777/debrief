{
  config,
  lib,
  pkgs,
  ...
}:

let
  cfg = config.programs.debrief;
  homeDir = config.home.homeDirectory;
  sessionVars = config.home.sessionVariables;
  isDarwin = pkgs.stdenv.hostPlatform.isDarwin;
  isLinux = pkgs.stdenv.hostPlatform.isLinux;
  exe = lib.getExe cfg.package;

  skills = [
    "ai-session"
    "ai-session-closeout"
  ];
  skillDir = skill: "${cfg.package}/share/debrief/agent/skills/${skill}";

  # Where Claude Code keeps CLAUDE.md, settings.json and skills. Home Manager's
  # claude-code module exports CLAUDE_CONFIG_DIR when this moves.
  defaultClaudeDir = "${homeDir}/.claude";
  claudeDir = config.programs.claude-code.configDir or defaultClaudeDir;

  # The instruction block exactly as `debrief install` renders it for the paths
  # this module uses. Rendered here rather than read from the package so that
  # evaluation never builds anything; the block-text check guards against drift.
  bundle = ../src/debrief/bundle;
  stripTrailingNewlines =
    s: if lib.hasSuffix "\n" s then stripTrailingNewlines (lib.removeSuffix "\n" s) else s;
  bundleVersion = stripTrailingNewlines (builtins.readFile (bundle + "/skills/ai-session/VERSION"));
  blockBody = stripTrailingNewlines (
    builtins.replaceStrings
      [ "<skills>" "<session>" ]
      [ "~/.agents/skills" "~/.local/bin/debrief-session" ]
      (builtins.readFile (bundle + "/block.md"))
  );
  blockText = "<!-- debrief:begin v${bundleVersion} -->\n${blockBody}\n<!-- debrief:end -->\n";

  # What `debrief install --claude-settings` adds to Claude Code's settings.json.
  sessionCmd = "${homeDir}/.local/bin/debrief-session";
  archiveDir =
    sessionVars.AI_SESSIONS_DIR or "${toString (sessionVars.XDG_DATA_HOME or "${homeDir}/.local/share")}/ai-sessions";
  claudeSettingsFragment = {
    hooks.SessionStart = [
      {
        hooks = [
          {
            type = "command";
            command = "${sessionCmd} context";
          }
        ];
      }
    ];
    permissions = {
      additionalDirectories = [ (toString archiveDir) ];
      allow =
        lib.concatMap
          (sub: map (form: "Bash(${form} ${sub}:*)") (lib.unique [ sessionCmd "~/.local/bin/debrief-session" "debrief-session" ]))
          [ "start" "context" "now" "changed" "close" "publish" ];
    };
  };

  # Debrief finds its archive, configuration and the agents' directories through
  # these variables. Activation and the service get the values shells get.
  debriefVars = [
    "AI_SESSIONS_DIR"
    "XDG_DATA_HOME"
    "XDG_CONFIG_HOME"
    "DEBRIEF_CONFIG_DIR"
    "DEBRIEF_HOME"
    "CODEX_HOME"
    "PI_CODING_AGENT_DIR"
  ];
  envExports = lib.concatStringsSep "\n" (
    lib.mapAttrsToList (name: value: ''export ${name}="${toString value}"'') (
      lib.filterAttrs (name: _: lib.elem name debriefVars) sessionVars
    )
    ++ [ ''export DEBRIEF_BIN_DIR="$HOME/.local/bin"'' ]
    ++ lib.optional (claudeDir != defaultClaudeDir) "export CLAUDE_CONFIG_DIR=${lib.escapeShellArg claudeDir}"
  );

  harnessArg = if cfg.agents == [ ] then "none" else lib.concatStringsSep "," cfg.agents;

  iniValue =
    v: if lib.isList v then lib.concatStringsSep ", " v else lib.generators.mkValueStringDefault { } v;
  configText = lib.generators.toINI {
    mkKeyValue = lib.generators.mkKeyValueDefault { mkValueString = iniValue; } " = ";
  } cfg.settings;

  # Services start with a minimal PATH; put the user's profile first so the
  # watcher runs the same git as their shell.
  serve = pkgs.writeShellScript "debrief-serve" ''
    ${envExports}
    export PATH="${config.home.profileDirectory}/bin''${PATH:+:$PATH}"
    exec ${exe} serve --watch
  '';
in
{
  options.programs.debrief = {
    enable = lib.mkEnableOption "Debrief, review of coding-agent work through the records agents keep";

    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.callPackage ./package.nix { };
      defaultText = lib.literalExpression "Debrief built with this configuration's pkgs";
      description = "The Debrief package.";
    };

    agents = lib.mkOption {
      type = lib.types.listOf (
        lib.types.enum [
          "claude"
          "codex"
          "devin"
          "pi"
        ]
      );
      default = [ ];
      example = [
        "claude"
        "codex"
      ];
      description = ''
        Coding agents that keep Debrief records. Each gets Debrief's always-on
        instruction block in its instruction file (Claude Code's CLAUDE.md, Codex's,
        Devin's and pi's AGENTS.md), and the skills are linked into
        {file}`~/.agents/skills` (and Claude Code's skills directory). Activation
        adds the block to the existing file, keeping the rest of it, and removes it
        from the files of agents not listed. Devin reads Claude Code's
        {file}`~/.claude/CLAUDE.md`, so with both listed it gets the block from there.
        An instruction file that Home Manager itself writes is left alone: include
        {option}`programs.debrief.blockText` in it.
      '';
    };

    claudeSettings = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = ''
        Whether activation adds Debrief's SessionStart hook, archive access and
        allow rules for {command}`debrief-session` to Claude Code's
        {file}`settings.json`, and removes them when this is turned off. If Home
        Manager writes that file ({option}`programs.claude-code.settings`), merge
        {option}`programs.debrief.claudeSettingsFragment` into it instead.
      '';
    };

    settings = lib.mkOption {
      type =
        with lib.types;
        attrsOf (
          attrsOf (oneOf [
            str
            int
            bool
            (listOf str)
          ])
        );
      default = { };
      example = lib.literalExpression ''
        {
          residency.allow_hosts = [ "git.corp.example" "*.corp.example" ];
          viewer.port = 7319;
        }
      '';
      description = ''
        Debrief's configuration, written to
        {file}`$XDG_CONFIG_HOME/debrief/config`. Lists become comma-separated
        values. When empty, Debrief keeps an editable default there instead.
      '';
    };

    service.enable = lib.mkEnableOption "the viewer as a user service ({command}`debrief serve --watch` on 127.0.0.1; systemd on Linux, launchd on macOS)";

    blockText = lib.mkOption {
      type = lib.types.str;
      readOnly = true;
      description = ''
        Debrief's instruction block, for instruction files Home Manager writes, for
        example `programs.claude-code.context = config.programs.debrief.blockText;`.
      '';
    };

    claudeSettingsFragment = lib.mkOption {
      type = lib.types.attrs;
      readOnly = true;
      description = ''
        The settings {option}`programs.debrief.claudeSettings` adds, for a
        {file}`settings.json` that Home Manager writes, for example
        `programs.claude-code.settings = config.programs.debrief.claudeSettingsFragment;`.
      '';
    };
  };

  config = lib.mkMerge [
    {
      programs.debrief = { inherit blockText claudeSettingsFragment; };
    }

    (lib.mkIf cfg.enable (
      lib.mkMerge [
        {
          assertions = [
            {
              assertion = cfg.claudeSettings -> lib.elem "claude" cfg.agents;
              message = ''programs.debrief.claudeSettings needs "claude" in programs.debrief.agents.'';
            }
          ];

          home.packages = [ cfg.package ];

          home.file = lib.mkIf (cfg.agents != [ ]) (
            {
              # The path the instructions give agents.
              ".local/bin/debrief-session".source = "${cfg.package}/bin/debrief-session";
            }
            // lib.listToAttrs (map (skill: lib.nameValuePair ".agents/skills/${skill}" { source = skillDir skill; }) skills)
            // lib.optionalAttrs (lib.elem "claude" cfg.agents) (
              lib.listToAttrs (map (skill: lib.nameValuePair "${claudeDir}/skills/${skill}" { source = skillDir skill; }) skills)
            )
          );

          xdg.configFile."debrief/config" = lib.mkIf (cfg.settings != { }) {
            text = "# Written by Home Manager from programs.debrief.settings.\n" + configText;
          };

          # A manual `debrief install` left a launcher and skill directories where
          # this module links its own, which would stop activation. Its zipapp marks
          # such an install; `debrief uninstall` removes exactly what it wrote and
          # keeps the archive. The step below then restores the blocks.
          home.activation.debriefMigrate = lib.hm.dag.entryBefore [ "checkLinkTargets" ] ''
            if ! (
              ${envExports}
              pyz="''${DEBRIEF_HOME:-''${XDG_DATA_HOME:-$HOME/.local/share}/debrief}/debrief.pyz"
              if [[ -f $pyz ]]; then
                echo "Debrief: removing what a manual \`debrief install\` wrote; Home Manager installs it now"
                run ${exe} uninstall
              fi
            ); then
              warnEcho "Debrief: could not remove a manual install; run 'debrief uninstall' and switch again"
            fi
          '';

          # Runs after the links are in place, so files the user's own configuration
          # links into the store are recognized and left alone.
          home.activation.debrief = lib.hm.dag.entryAfter [ "linkGeneration" ] ''
            if ! (
              ${envExports}
              run ${exe} install --managed --quiet --harness ${harnessArg}${lib.optionalString cfg.claudeSettings " --claude-settings"}
            ); then
              warnEcho "Debrief: could not update the agents' instruction files"
            fi
          '';
        }

        (lib.mkIf (cfg.service.enable && isLinux) {
          systemd.user.services.debrief = {
            Unit.Description = "Debrief viewer and watcher (127.0.0.1 only)";
            Service = {
              ExecStart = "${serve}";
              Restart = "on-failure";
              RestartSec = 10;
            };
            Install.WantedBy = [ "default.target" ];
          };
        })

        (lib.mkIf (cfg.service.enable && isDarwin) {
          launchd.agents.debrief = {
            enable = true;
            config = {
              ProgramArguments = [ "${serve}" ];
              RunAtLoad = true;
              KeepAlive.SuccessfulExit = false;
              ThrottleInterval = 10;
              StandardOutPath = "${homeDir}/Library/Logs/debrief.log";
              StandardErrorPath = "${homeDir}/Library/Logs/debrief.log";
            };
          };
        })
      ]
    ))
  ];
}
