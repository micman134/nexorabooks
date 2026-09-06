; Nexora Books — Windows installer
;
; Built by build_windows.bat when Inno Setup is present. Inno Setup is free:
;   https://jrsoftware.org/isdl.php
;
; What this deliberately does:
;
;   * puts the application in Program Files and makes the shortcuts, so it is
;     installed the way every other Windows program is and appears in
;     "Apps & features" like one;
;   * offers to open the firewall for the private network, because without that
;     the office cannot reach it and the first support call is always this;
;   * leaves the customer's books completely alone when it uninstalls. Their
;     accounts are in AppData and are not the installer's to delete. Somebody
;     uninstalling to reinstall a newer version must not lose their ledger, and
;     an uninstaller that deletes accounting records is indefensible.

#define AppName        "Nexora Books"
#define AppExe         "NexoraBooks.exe"

; build_windows.bat passes the real version in with /DAppVersion=..., read
; straight out of app\config.py, so the installer's name can never drift out
; of step with what the software reports about itself. The fallback below is
; only for compiling this file by hand.
#ifndef AppVersion
  #define AppVersion   "0.0.0-hand-built"
#endif

; AppPublisher is the name Windows shows in "Apps & features" and on the
; security prompt. It must match the business name on your code-signing
; certificate exactly, so change it here and on the certificate together.
#define AppPublisher   "Tavo Networks Limited"
; AppUrl is shown in "Apps & features" as the support link. Point it at the
; product's own page once there is one; until then, the support address.
#define AppUrl         "mailto:support@tavonetworks.tech"

[Setup]
AppId={{7C2E5A41-9E1B-4B27-9E0B-2F5A1D8C4A10}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppSupportURL={#AppUrl}
AppUpdatesURL={#AppUrl}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=NexoraBooks-{#AppVersion}-Setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
LicenseFile=..\LICENCE-AGREEMENT.txt
InfoAfterFile=..\installer\after-install.txt
UninstallDisplayIcon={app}\{#AppExe}
SetupIconFile=..\app\static\nexorabooks.ico
; Admin is wanted for the firewall rule and for Program Files, but somebody
; without it can still install for themselves rather than being turned away.
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=dialog commandline

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a shortcut on the desktop"; \
  GroupDescription: "Shortcuts:"
Name: "firewall"; Description: "Allow staff on this office network to reach it"; \
  GroupDescription: "Network:"; Check: IsAdminInstallMode

[Files]
Source: "..\dist\NexoraBooks\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\README.md";   DestDir: "{app}"; DestName: "README.txt"; Flags: ignoreversion isreadme
Source: "..\INSTALL.txt"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
; The agreement says at clause 14 that these three documents together are the
; whole agreement, so all three have to reach the customer. Shipping the
; agreement alone would leave it pointing at documents nobody received.
Source: "..\LICENCE-AGREEMENT.txt"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
Source: "..\REFUNDS.txt"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
Source: "..\PRIVACY.txt"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist

[Icons]
Name: "{group}\{#AppName}";        Filename: "{app}\{#AppExe}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}";  Filename: "{app}\{#AppExe}"; Tasks: desktopicon
; The way back in when the only administrator has lost the phone that makes
; their six-digit codes. Every other route needs somebody to be signed in, so
; this one has to be reachable from the Start menu rather than from inside.
Name: "{group}\Reset two-factor sign-in"; Filename: "{app}\{#AppExe}"; \
  Parameters: "--reset-two-factor"; Comment: "Locked out by two-factor sign-in? Start here."

[Run]
; Two rules, because Windows treats the two protocols separately and the
; application listens on TCP but is found by name over UDP on some networks.
Filename: "{sys}\netsh.exe"; \
  Parameters: "advfirewall firewall add rule name=""{#AppName}"" dir=in action=allow program=""{app}\{#AppExe}"" enable=yes profile=private protocol=tcp"; \
  Flags: runhidden; Tasks: firewall; StatusMsg: "Opening the office network..."
Filename: "{sys}\netsh.exe"; \
  Parameters: "advfirewall firewall add rule name=""{#AppName}"" dir=in action=allow program=""{app}\{#AppExe}"" enable=yes profile=private protocol=udp"; \
  Flags: runhidden; Tasks: firewall

Filename: "{app}\{#AppExe}"; Description: "Start {#AppName} now"; \
  Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\netsh.exe"; \
  Parameters: "advfirewall firewall delete rule name=""{#AppName}"""; \
  Flags: runhidden; RunOnceId: "RemoveFirewallRule"

[UninstallDelete]
; Only what the installer itself put there. Never the data folder — a
; customer's accounts are not ours to remove.
Type: filesandordirs; Name: "{app}\_internal"

[Messages]
; The last screen normally talks about the program. This one has to talk about
; the customer's books, because that is what they will be worrying about.
ConfirmUninstall=Are you sure you want to remove %1?%n%nYour accounts will NOT be deleted. They stay in your data folder and will still be there if you install it again.
