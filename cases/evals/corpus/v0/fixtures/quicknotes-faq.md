# QuickNotes Frequently Asked Questions

## What is QuickNotes?

QuickNotes is a local-first note-taking application that stores all your data
on your own device. It supports Markdown editing, wiki-style linking between
notes, and full-text search. No account registration or internet connection
is required.

## How do I install QuickNotes?

Download the installer from the official website for your platform.
On Windows, run the .msi installer. On macOS, drag the .dmg to Applications.
On Linux, use the .AppImage or .deb package.

## Where are my notes stored?

In QuickNotes v1.0: ~/.quicknotes/
In QuickNotes v2.0: ~/Documents/QuickNotes/

Your notes are stored as plain Markdown (.md) files in this directory.
You can back them up by simply copying the entire directory.
In v2.0, automatic daily backups are also created in the backups/ subdirectory.

## Can I use QuickNotes offline?

Yes. QuickNotes is designed to work entirely offline. There is no cloud
dependency. All features—editing, search, tagging, export—work without
an internet connection.

## How do I create a link between two notes?

Use the [[wiki-link]] syntax. Type [[ followed by the title of the target
note. QuickNotes will auto-complete as you type. Links are bidirectional:
the target note will show a "Linked mentions" section.
In v2.0, you can also use display text syntax: [[note|Display Text]].

## How does encryption work?

QuickNotes v2.0 supports AES-256 encryption for individual notes. To encrypt
a note, right-click it and select "Encrypt". You will be prompted to set a
password. Encrypted notes appear with a lock icon and require the password
to view or edit. The .md file on disk contains ciphertext, not plaintext.
Encryption is a v2.0 feature and is not available in v1.0.

## Does QuickNotes support cloud sync?

No. QuickNotes is intentionally local-first. There is no cloud sync, no
account system, and no remote server. If you need to sync notes across
devices, you can store the QuickNotes data directory in a third-party
sync folder (e.g., Dropbox, Syncthing), but this is not officially
supported and may cause conflicts if multiple instances write simultaneously.

## How do I export my notes?

In v1.0, export is limited to Markdown format.
In v2.0, you can export notes in Markdown, PDF, HTML, and Plain Text formats.
Go to File > Export and select your preferred format. In v2.0, you can also
batch-export multiple notes at once by selecting them in the file explorer.

## What file formats can I import?

- v1.0: Plain text (.txt) and Markdown (.md) files
- v2.0: .txt, .md, .html, and .docx files

Drag and drop files into the QuickNotes window, or use File > Import.

## What Markdown features are supported?

In v1.0, QuickNotes uses the CommonMark specification (standard Markdown).
In v2.0, it uses GitHub Flavored Markdown (GFM), which adds task lists,
strikethrough, autolinks, and footnotes. Both versions support fenced code
blocks, tables, images, and headings.

## Is there a mobile version?

Not currently. QuickNotes is available for Windows, macOS, and Linux desktop
platforms only.

## How do I create a tag?

Use the # symbol followed by the tag name. Tags can be hierarchical:
- In v1.0: #work/project (using '/' as delimiter)
- In v2.0: #work:project (using ':' as delimiter)

In v2.0, tag autocomplete uses fuzzy matching—typing "#pro" will suggest
"#work:project".

## How do I customize the appearance?

Go to Settings > Appearance. QuickNotes offers multiple built-in themes:
Light, Dark, Sepia, Nord (v2.0+), and Dracula (v2.0+).
In v2.0, the default theme follows your operating system setting (light/dark).
You can also customize the font family, font size, line spacing, and add
custom CSS snippets (v2.0+).

## How does auto-save work?

- v1.0: Notes are saved every 5 minutes. Changes between saves may be lost
  if the application crashes.
- v2.0: Notes are saved every 30 seconds, and also when switching tabs or
  minimizing the window. Crash recovery files are written during auto-save.

## How do I search across all my notes?

- v1.0: Search is limited to the currently open note (Ctrl+F).
- v2.0: Use Ctrl+F to search the current note, or Ctrl+Shift+F to search
  across all notes. v2.0 also supports regular expressions, boolean operators
  (AND, OR, NOT), case-sensitive search, and exact phrase matching.

## How do I restore a previous version of a note?

In v2.0, QuickNotes keeps the last 3 versions of each note. Right-click a
note and choose "View Version History" to browse, diff, or restore a previous
version. v1.0 does not have file versioning.

## What keyboard shortcuts are available?

Common shortcuts:
- Ctrl+S: Save
- Ctrl+F: Find
- Ctrl+N: New note
- Ctrl+W: Close tab
- Ctrl+B/I: Bold/Italic
- F11: Fullscreen

Note: In v1.0, Ctrl+P opens Quick File Open. In v2.0, Ctrl+P opens the
Command Palette, and Quick File Open is now Ctrl+Shift+O.

## Does QuickNotes support plugins or extensions?

No. QuickNotes does not have a plugin marketplace or extension API. All
features are built-in.

## How do I migrate from v1.0 to v2.0?

Install v2.0 and launch it. Notes from ~/.quicknotes/ are automatically
copied to ~/Documents/QuickNotes/. The old directory is kept as a backup.
Note: tag delimiters (changed from '/' to ':') and keyboard shortcuts
(Ctrl+P reassigned) are breaking changes that require manual action.
See the v2.0 release notes for a full migration checklist.
