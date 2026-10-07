{
  lib,
  stdenvNoCC,
  python3,
  makeWrapper,
  git,
  openssh,
  openssl,
}:

let
  # The version is kept in one place: the Python package.
  versionOf =
    line:
    let
      m = builtins.match ''__version__ = "([^"]+)"'' line;
    in
    if m == null then null else builtins.head m;
  version = lib.findFirst (v: v != null) (throw "no __version__ in src/debrief/__init__.py") (
    map versionOf (lib.splitString "\n" (builtins.readFile ../src/debrief/__init__.py))
  );

  # Debrief runs git for evidence and sync, ssh for git remotes and openssl for
  # the hub's certificate. Appended to PATH, so the user's own tools come first.
  runtimePath = lib.makeBinPath [
    git
    openssh
    openssl
  ];
in
stdenvNoCC.mkDerivation {
  pname = "debrief";
  inherit version;

  src = lib.fileset.toSource {
    root = ../.;
    fileset = lib.fileset.unions [
      ../build.py
      ../src
    ];
  };

  nativeBuildInputs = [
    python3
    makeWrapper
  ];

  # Debrief is standard-library Python, packed into one zipapp. The build also
  # renders the agent instructions and skills for the paths the home-manager
  # module installs them at (~/.local/bin/debrief-session, ~/.agents/skills).
  buildPhase = ''
    runHook preBuild
    python3 build.py dist/debrief.pyz
    env -u DEBRIEF_BIN_DIR HOME="$TMPDIR/render-home" python3 dist/debrief.pyz install --render agent
    runHook postBuild
  '';

  installPhase = ''
    runHook preInstall
    install -Dm644 dist/debrief.pyz $out/share/debrief/debrief.pyz
    cp -r agent $out/share/debrief/agent
    makeWrapper ${python3.interpreter} $out/bin/debrief \
      --add-flag $out/share/debrief/debrief.pyz \
      --suffix PATH : ${runtimePath}
    makeWrapper ${python3.interpreter} $out/bin/debrief-session \
      --add-flag $out/share/debrief/debrief.pyz --add-flag session \
      --suffix PATH : ${runtimePath}
    runHook postInstall
  '';

  doInstallCheck = true;
  installCheckPhase = ''
    runHook preInstallCheck
    $out/bin/debrief --version | grep -qx "debrief ${version}"
    $out/bin/debrief-session --help > /dev/null
    grep -qF '`bin/session` below means `~/.local/bin/debrief-session`' $out/share/debrief/agent/block.md
    grep -qF '`bin/session` means `~/.local/bin/debrief-session`' $out/share/debrief/agent/skills/ai-session/SKILL.md
    if grep -rq render-home $out/share/debrief/agent; then
      echo "rendered agent files mention the build's HOME" >&2
      exit 1
    fi
    runHook postInstallCheck
  '';

  meta = {
    description = "Review coding-agent work through the records agents keep, checked against git";
    homepage = "https://github.com/numine777/debrief";
    license = lib.licenses.asl20;
    mainProgram = "debrief";
    platforms = lib.platforms.unix;
  };
}
