// BMeet.app's own texts. English is the text in the source: L("Dictate"). Russian is
// macos/app/ru.lproj/Localizable.strings, with the English text as the key; macos/tools/
// check-strings.py checks the two against each other, in CI. The language follows
// ~/.config/bmeet/interface-language ("system", "en", "ru") and changes while BMeet runs:
// Interface.reload() reads it again (AppDelegate.settingsChanged), and the menu and an open
// Settings window are built again in the new one.
//
// What macOS shows itself stays in the Mac's language: the app's name, the titles of Finder's
// services (ru.lproj/ServicesMenu.strings, for a Mac whose language is Russian), the permission
// prompts (ru.lproj/InfoPlist.strings), and AppKit's own texts such as a text field's menu.

import Foundation

enum Interface {
    /// The languages BMeet.app comes in, each named in itself. The first is the fallback.
    static let languages: [(code: String, name: String)] = [("en", "English"), ("ru", "Русский")]

    /// A check's --lang: this run's language, whatever the settings say.
    static var forced: String?
    /// The language of the texts now: "en" or "ru".
    private(set) static var language = "en"
    private static var table: [String: String] = [:]
    private static var loaded = false
    /// Texts asked for in a language that has no line for them (a check reports them).
    private(set) static var missing = Set<String>()

    /// The language `choice` stands for: "system" is the first of the Mac's languages (or the one
    /// chosen for BMeet alone in System Settings) that BMeet comes in, else English.
    static func resolve(_ choice: String) -> String {
        if languages.contains(where: { $0.code == choice }) { return choice }
        for preferred in Locale.preferredLanguages {
            let code = Locale(identifier: preferred).language.languageCode?.identifier ?? preferred
            if languages.contains(where: { $0.code == code }) { return code }
        }
        return languages[0].code
    }

    /// Reads the setting (and the table) again; true if the language changed.
    @discardableResult
    static func reload() -> Bool {
        let now = forced ?? resolve(Settings.interfaceLanguage)
        guard now != language || !loaded else { return false }
        loaded = true
        language = now
        table = now == "en" ? [:] : loadTable(now)
        missing = []
        return true
    }

    /// The table in BMeet.app's Resources/<lang>.lproj, or in the program files (a build run
    /// from the command line, the tests: $BMEET_SRC/macos/app/<lang>.lproj).
    private static func loadTable(_ code: String) -> [String: String] {
        let candidates = [
            Bundle.main.url(forResource: "Localizable", withExtension: "strings", subdirectory: nil, localization: code),
            Paths.src.appendingPathComponent("macos/app/\(code).lproj/Localizable.strings"),
        ]
        for url in candidates.compactMap({ $0 }) {
            if let table = NSDictionary(contentsOf: url) as? [String: String] { return table }
        }
        NSLogLine("no \(code) texts (\(code).lproj/Localizable.strings): English instead")
        return [:]
    }

    /// Forgets the texts that had no line (a check, before it builds a page again).
    static func clearMissing() { missing = [] }

    static func lookUp(_ english: String) -> String {
        guard language != "en" else { return english }
        if let text = table[english] { return text }
        missing.insert(english)
        return english
    }

    /// The locale of the texts: language names and dates are written in it.
    static var locale: Locale { Locale(identifier: language) }

    /// A language's name in the interface language, as a list shows it ("ru" → "Русский").
    static func languageName(_ code: String) -> String {
        // (Whisper says "jw" for Javanese, which is "jv" elsewhere)
        let name = locale.localizedString(forLanguageCode: code == "jw" ? "jv" : code) ?? code
        return name.prefix(1).uppercased() + name.dropFirst()
    }
}

/// `english` in the interface language.
func L(_ english: String) -> String { Interface.lookUp(english) }

/// The same for a text with %@ (a text) or %d (a number) in it. A translation that needs them
/// in another order writes %1$@, %2$@.
func L(_ english: String, _ arguments: CVarArg...) -> String {
    String(format: Interface.lookUp(english), locale: Interface.locale, arguments: arguments)
}

/// Whisper's language codes (openai/whisper tokenizer.py LANGUAGES; voxtype/user-setup.sh has
/// the same list): the dictation languages Settings → Languages offers.
let whisperLanguages = """
    en zh de es ru ko fr ja pt tr pl ca nl ar sv it id hi fi vi he uk el ms cs ro da hu ta no th ur hr bg lt la mi ml
    cy sk te fa lv bn sr az sl kn et mk br eu is hy ne mn bs kk sq sw gl mr pa si km sn yo so af oc ka be tg sd gu am
    yi lo uz fo ht ps tk nn mt sa lb my bo tl mg as tt haw ln ha ba jw su yue
    """.split(whereSeparator: { $0 == " " || $0 == "\n" }).map(String.init)
