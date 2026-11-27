"""bmeettext - the texts a person sees in BMeet's Python programs, in the interface language.

One table for the BMeet window (bmeet/app/bmeet) and the notifications of macos/bmeet-meeting:
each text is written in English where it is used, inside tr("..."), and RU below has its
Russian line. bmeet/app/tests/test_bmeet.py reads both programs and fails when a text has no
Russian line or a Russian line no text. Command-line output (npu-transcribe, bmeet-ctl) and the
contents of transcript files (You, Person 1, the headings) stay English. Stdlib only: bmeet-ctl
imports it on the system Python too.

The interface language comes from ~/.config/bmeet/interface-language: "system" (the default),
"en" or "ru". "system" is the first of the system's preferred languages that BMeet has, else
English: on a Mac the list of System Settings → General → Language & Region
(CFLocaleCopyPreferredLanguages, the list behind Swift's Locale.preferredLanguages), on Linux
the locale (LC_ALL, else LC_MESSAGES, else LANG).
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

LANGUAGES = ("en", "ru")   # the interface languages BMeet has
MAC = sys.platform == "darwin" and os.environ.get("BMEET_PLATFORM") != "linux"   # (tests: Linux on a Mac)


def config_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "bmeet"


def setting() -> str:
    """~/.config/bmeet/interface-language: system, en or ru (anything else: system)."""
    try:
        word = (config_dir() / "interface-language").read_text().split()[0].lower()
    except (OSError, IndexError, UnicodeDecodeError):
        return "system"
    return word if word in LANGUAGES else "system"


def _mac_languages() -> list[str]:
    """The Mac's preferred languages, most preferred first ("ru-RU" -> "ru"); [] if unknown."""
    import ctypes
    try:
        cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    except OSError:
        return []
    cf.CFLocaleCopyPreferredLanguages.restype = ctypes.c_void_p
    cf.CFArrayGetCount.argtypes = [ctypes.c_void_p]
    cf.CFArrayGetCount.restype = ctypes.c_long
    cf.CFArrayGetValueAtIndex.argtypes = [ctypes.c_void_p, ctypes.c_long]
    cf.CFArrayGetValueAtIndex.restype = ctypes.c_void_p
    cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
    cf.CFStringGetCString.restype = ctypes.c_bool
    cf.CFRelease.argtypes = [ctypes.c_void_p]
    array = cf.CFLocaleCopyPreferredLanguages()
    if not array:
        return []
    try:
        codes, buf = [], ctypes.create_string_buffer(64)
        for i in range(cf.CFArrayGetCount(array)):
            item = cf.CFArrayGetValueAtIndex(array, i)
            if item and cf.CFStringGetCString(item, buf, len(buf), 0x08000100):   # (kCFStringEncodingUTF8)
                codes.append(re.split(r"[-_]", buf.value.decode())[0].lower())
        return codes
    finally:
        cf.CFRelease(array)


def system_languages() -> list[str]:
    """The system's preferred languages as codes, most preferred first: a Mac's list, or on
    Linux the locale's one (and a Mac's own locale variables when its list can't be read)."""
    if MAC and (codes := _mac_languages()):
        return codes
    for name in ("LC_ALL", "LC_MESSAGES", "LANG"):
        if value := os.environ.get(name):
            return [re.split(r"[_.@]", value)[0].lower()]
    return []


def interface_language() -> str:
    """The language BMeet speaks now: en or ru."""
    chosen = setting()
    if chosen != "system":
        return chosen
    return next((code for code in system_languages() if code in LANGUAGES), "en")


_current: str | None = None   # (None: not chosen yet; the first text decides)


def use(language: str) -> str:
    """Speak `language` from now on (the window, when it starts); returns it (en if unknown)."""
    global _current
    _current = language if language in LANGUAGES else "en"
    return _current


def current() -> str:
    if _current is None:
        use(interface_language())
    return _current


def N_(text: str) -> str:
    """A text to translate where it is shown (with tr()), not where it is written."""
    return text


def tr(text: str, **values) -> str:
    """`text` in the interface language, with {name} filled in from `values`."""
    line = RU.get(text, text) if current() == "ru" else text
    if isinstance(line, tuple):   # (a number's forms, used without one)
        line = line[2]
    return line.format(**values) if values else line


def ru_form(n: int) -> int:
    """Which of Russian's three forms a number takes: 1 файл, 2 файла, 5 файлов."""
    if n % 10 == 1 and n % 100 != 11:
        return 0
    return 1 if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else 2


def ntr(n: int, one: str, many: str, **values) -> str:
    """"{n} file" or "{n} files" in the interface language; RU[one] has Russian's three forms."""
    if current() == "ru" and isinstance(RU.get(one), tuple):
        line = RU[one][ru_form(n)]
    else:
        line = one if n == 1 else many
    return line.format(n=n, **values)


def language_name(code: str, sentence: bool = False) -> str | None:
    """A Whisper language's name in Russian ("de" -> "Немецкий"; "немецкий" in a sentence), or
    None when the interface is English (Qt names it there)."""
    if current() != "ru":
        return None
    name = RU_LANGUAGES.get(code)
    if name is None:
        return code.upper()
    return name if sentence else name[0].upper() + name[1:]


# Whisper's languages in Russian (Qt names a language only in English or in itself)
RU_LANGUAGES = {
    "af": "африкаанс", "am": "амхарский", "ar": "арабский", "as": "ассамский", "az": "азербайджанский",
    "ba": "башкирский", "be": "белорусский", "bg": "болгарский", "bn": "бенгальский", "bo": "тибетский",
    "br": "бретонский", "bs": "боснийский", "ca": "каталанский", "cs": "чешский", "cy": "валлийский",
    "da": "датский", "de": "немецкий", "el": "греческий", "en": "английский", "es": "испанский",
    "et": "эстонский", "eu": "баскский", "fa": "персидский", "fi": "финский", "fo": "фарерский",
    "fr": "французский", "gl": "галисийский", "gu": "гуджарати", "ha": "хауса", "haw": "гавайский",
    "he": "иврит", "hi": "хинди", "hr": "хорватский", "ht": "гаитянский креольский", "hu": "венгерский",
    "hy": "армянский", "id": "индонезийский", "is": "исландский", "it": "итальянский", "ja": "японский",
    "jw": "яванский", "ka": "грузинский", "kk": "казахский", "km": "кхмерский", "kn": "каннада",
    "ko": "корейский", "la": "латинский", "lb": "люксембургский", "ln": "лингала", "lo": "лаосский",
    "lt": "литовский", "lv": "латышский", "mg": "малагасийский", "mi": "маори", "mk": "македонский",
    "ml": "малаялам", "mn": "монгольский", "mr": "маратхи", "ms": "малайский", "mt": "мальтийский",
    "my": "бирманский", "ne": "непальский", "nl": "нидерландский", "nn": "нюнорск", "no": "норвежский",
    "oc": "окситанский", "pa": "панджаби", "pl": "польский", "ps": "пушту", "pt": "португальский",
    "ro": "румынский", "ru": "русский", "sa": "санскрит", "sd": "синдхи", "si": "сингальский",
    "sk": "словацкий", "sl": "словенский", "sn": "шона", "so": "сомали", "sq": "албанский",
    "sr": "сербский", "su": "сунданский", "sv": "шведский", "sw": "суахили", "ta": "тамильский",
    "te": "телугу", "tg": "таджикский", "th": "тайский", "tk": "туркменский", "tl": "тагальский",
    "tr": "турецкий", "tt": "татарский", "uk": "украинский", "ur": "урду", "uz": "узбекский",
    "vi": "вьетнамский", "yi": "идиш", "yo": "йоруба", "yue": "кантонский", "zh": "китайский",
}

# The Russian lines, by their English text. A text that names Linux's keys or Dolphin has a
# second line for a Mac, where the window writes ⌃⌥, ⌘ and Finder (mac_words in bmeet/app/bmeet).
# A number's text has its three forms: 1, 2-4, 5 (ntr). Words as in BMeet.app's Russian.
RU: dict[str, str | tuple[str, str, str]] = {
    # -- the window: pages, lists, dialogs
    "Meetings": "Встречи",
    "Transcribed files": "Расшифрованные файлы",
    "Transcribe a file": "Расшифровать файл",
    "This window switches to English when it opens again.": "Окно перейдет на английский, когда откроется снова.",
    "This window switches to Russian when it opens again.": "Окно перейдет на русский, когда откроется снова.",
    "All files": "Все файлы",
    "Audio and video files": "Аудио- и видеофайлы",
    "Plain text": "Обычный текст",
    "empty": "пустое",
    "longer than 40 characters": "длиннее 40 символов",
    "reserved, or contains one of # * , [ ] ` < > |": "зарезервировано или содержит один из символов # * , [ ] ` < > |",
    "Failed": "Ошибка",
    "Done": "Готово",
    "Files missing": "Файлы не найдены",
    "OK": "OK",
    "Cancel": "Отмена",
    "Close": "Закрыть",
    "Yes": "Да",
    "No": "Нет",
    "Drop audio or video files here": "Перетащите сюда аудио- или видеофайлы",
    "Choose files…": "Выбрать файлы…",
    "Nothing matches the search.": "Ничего не найдено.",
    "Select an item to see its transcript.": "Выберите строку, чтобы увидеть расшифровку.",
    "Copied the transcript to the clipboard": "Расшифровка скопирована в буфер обмена",
    "Name speakers": "Имена собеседников",
    "Who is who? Leave a field empty to keep the label. Give two labels the same name when they are one person.":
        "Кто есть кто? Оставьте поле пустым, чтобы сохранить метку. Если две метки — один человек, дайте им одно имя.",
    "This meeting was in person: write You for yourself.": "Это была очная встреча: себя назовите You.",
    "Name": "Имя",
    "Recognise them by voice in later meetings": "Узнавать их по голосу на следующих встречах",
    "Keeps a voice fingerprint of each person you name on this computer ({folder}), and later meetings call "
    "them by name.\nUnticking it forgets all fingerprints.":
        "Хранит на этом компьютере ({folder}) голосовой отпечаток каждого, кого вы назвали, и на следующих "
        "встречах их называют по имени.\nЕсли снять флажок, все отпечатки забываются.",
    "Remembered voices…": "Запомненные голоса…",
    "Recognition starts with the next meeting: this one was transcribed before remembering voices was on, so "
    "it has no fingerprints.":
        "Узнавание начнется со следующей встречи: эту расшифровали до того, как включили запоминание голосов, "
        "и отпечатков у нее нет.",
    "Forget all remembered voices?": "Забыть все запомненные голоса?",
    "Forget voices": "Забыть голоса",
    "Not a name: {problems}": "Не подходит как имя: {problems}",
    "Remembered voices": "Запомненные голоса",
    "Voice fingerprints kept in {folder}. Later meetings call these people by name.":
        "Голосовые отпечатки хранятся в {folder}. На следующих встречах этих людей называют по имени.",
    "Forget": "Забыть",
    "You: your own voice  ({meetings})": "You: ваш собственный голос  ({meetings})",
    "{n} meeting": ("{n} встреча", "{n} встречи", "{n} встреч"),
    "Learned from your microphone in online meetings; in-person meetings use it to tell you from the others "
    "in the room":
        "Выучен по вашему микрофону на онлайн-встречах; на очных встречах по нему вас отличают от остальных "
        "в комнате",
    "Title": "Название",
    "Date": "Дата",
    "Duration": "Длительность",
    "Status": "Состояние",
    "Active": "Идет",
    "Completed": "Завершена",
    "Interrupted": "Прервана",
    "Recording": "Запись",
    "Paused": "Пауза",
    "No meetings yet.\n\nPress “Start meeting” above (or Meta+Alt+M) to record the microphone and the system "
    "audio. The transcript shows up here as people talk.":
        "Встреч пока нет.\n\nНажмите «Начать встречу» вверху (или Meta+Alt+M), чтобы записывать микрофон и звук "
        "системы. Расшифровка появляется здесь по ходу разговора.",
    "No meetings yet.\n\nPress “Start meeting” above (or ⌃⌥M) to record the microphone and the system "
    "audio. The transcript shows up here as people talk.":
        "Встреч пока нет.\n\nНажмите «Начать встречу» вверху (или ⌃⌥M), чтобы записывать микрофон и звук "
        "системы. Расшифровка появляется здесь по ходу разговора.",
    "No meetings yet.\n\nPress “In person” above (or Meta+Alt+Shift+M) to record the microphone and tell the "
    "voices in the room apart.":
        "Встреч пока нет.\n\nНажмите «Очная встреча» вверху (или Meta+Alt+Shift+M), чтобы записывать микрофон "
        "и различать голоса в комнате.",
    "No meetings yet.\n\nPress “In person” above (or ⌃⌥⇧M) to record the microphone and tell the "
    "voices in the room apart.":
        "Встреч пока нет.\n\nНажмите «Очная встреча» вверху (или ⌃⌥⇧M), чтобы записывать микрофон "
        "и различать голоса в комнате.",
    "No meetings yet.": "Встреч пока нет.",
    "Reload": "Загрузить заново",
    "Load the transcript again from {engine}": "Загрузить расшифровку из {engine} заново",
    "Copy all": "Копировать все",
    "Copy the whole transcript (Markdown) to the clipboard": "Скопировать всю расшифровку (Markdown) в буфер обмена",
    "Export…": "Экспорт…",
    "Save the transcript as Markdown, plain text or JSON": "Сохранить расшифровку как Markdown, обычный текст или JSON",
    "Open folder": "Открыть папку",
    "Show the Markdown copy in the file manager": "Показать копию Markdown в файловом менеджере",
    "Show the Markdown copy in Finder": "Показать копию Markdown в Finder",
    "Name speakers…": "Имена собеседников…",
    "Put names on Person 1, Person 2, …; optionally recognise them by voice in later meetings":
        "Дать имена Person 1, Person 2, …; по желанию узнавать их по голосу на следующих встречах",
    "Delete…": "Удалить…",
    "Delete this meeting from {engine}": "Удалить эту встречу из {engine}",
    "Cannot read {path}: {error}": "Не удается прочитать {path}: {error}",
    "{n} chunk": ("{n} фрагмент", "{n} фрагмента", "{n} фрагментов"),
    "Markdown copy: {path}": "Копия Markdown: {path}",
    "From {engine}'s meeting database (no Markdown copy)": "Из базы встреч {engine} (без копии Markdown)",
    "Telling the speakers apart… the transcript is replaced when that is done.":
        "Различаем собеседников… Когда это закончится, расшифровка будет заменена.",
    "Live: transcribed as people talk; in italics, what is still being said":
        "Расшифровка по ходу встречи; курсивом — то, что еще говорится",
    "Wait until the speakers are told apart": "Подождите, пока закончится различение собеседников",
    "This transcript has no speakers to name": "В этой расшифровке нет собеседников, которым можно дать имена",
    "Could not rename the speakers": "Не удалось переименовать собеседников",
    "Renamed {names}; {people} will be recognised in later meetings":
        "Переименовано: {names}; на следующих встречах будут узнаваться: {people}",
    "Renamed {names}": "Переименовано: {names}",
    "Stop the meeting before deleting it": "Остановите встречу, прежде чем удалять ее",
    "Loading the transcript…": "Загрузка расшифровки…",
    "{engine} meeting export": "Экспорт встречи из {engine}",
    "Could not load the transcript": "Не удалось загрузить расшифровку",
    "{engine} exited with status {code}": "{engine} завершился с кодом {code}",
    "Is the {engine} daemon running?": "Запущен ли демон {engine}?",
    "The transcript shows up here as people talk": "Расшифровка появляется здесь по ходу разговора",
    "If it does not, the live transcript is off, and the transcript shows up here when the meeting stops. It "
    "needs telling speakers apart (on unless installed with --no-speakers), and "
    "~/.config/bmeet/live-transcript must not say “off”.":
        "Если нет, расшифровка по ходу встречи выключена, и расшифровка появится здесь, когда встреча "
        "закончится. Для нее нужно различение собеседников (включено, если BMeet установлен без --no-speakers), "
        "и в ~/.config/bmeet/live-transcript не должно быть «off».",
    "*(nothing said yet)*": "*(пока ничего не сказано)*",
    "*(the transcript is empty)*": "*(расшифровка пуста)*",
    "Export meeting transcript": "Экспорт расшифровки встречи",
    "Export failed": "Не удалось экспортировать",
    "Exported to {path}": "Экспортировано в {path}",
    "Delete meeting": "Удалить встречу",
    "Delete “{title}” ({date})?": "Удалить «{title}» ({date})?",
    "The meeting and its transcript are removed from {engine}'s database. This cannot be undone.":
        "Встреча и ее расшифровка будут удалены из базы {engine}. Это нельзя отменить.",
    "Also move the Markdown copy “{name}” to the Trash": "Также переместить копию Markdown «{name}» в Корзину",
    "Delete": "Удалить",
    "Delete failed": "Не удалось удалить",
    "Deleted “{title}”": "Удалено: «{title}»",
    "Deleted “{title}”; its Markdown copy is in the Trash": "Удалено: «{title}»; копия Markdown в Корзине",
    "Could not move to the Trash": "Не удалось переместить в Корзину",
    "Model": "Модель",
    "No transcribed files yet.\n\nUse “Transcribe a file”, or right-click a recording in Dolphin → "
    "Transcribe (NPU).":
        "Расшифрованных файлов пока нет.\n\nОткройте «Расшифровать файл» или щелкните запись правой кнопкой "
        "в Dolphin → Transcribe (NPU).",
    "No transcribed files yet.\n\nUse “Transcribe a file”, or right-click a recording in Finder → Quick "
    "Actions → Transcribe with BMeet.":
        "Расшифрованных файлов пока нет.\n\nОткройте «Расшифровать файл» или щелкните запись правой кнопкой "
        "в Finder → Быстрые действия.",
    "No transcribed files yet.": "Расшифрованных файлов пока нет.",
    "Copy": "Копировать",
    "Copy the transcript to the clipboard": "Скопировать расшифровку в буфер обмена",
    "Open subtitles": "Открыть субтитры",
    "Open the .srt subtitles": "Открыть субтитры .srt",
    "Open notes": "Открыть заметки",
    "Open the .md notes in your editor": "Открыть заметки .md в редакторе",
    "Show the transcript in the file manager": "Показать расшифровку в файловом менеджере",
    "Show the transcript in Finder": "Показать расшифровку в Finder",
    "Transcribe again": "Расшифровать снова",
    "Queue the audio/video file on “Transcribe a file”": "Поставить аудио- или видеофайл в очередь «Расшифровать файл»",
    "Remove from list…": "Убрать из списка…",
    "Remove from this list (files stay unless you choose otherwise)":
        "Убрать из этого списка (файлы остаются, если не выбрать иное)",
    "{model} on {device}": "{model} на {device}",
    "language {language}": "язык {language}",
    "Transcription failed": "Расшифровка не удалась",
    "Use “Transcribe again” to retry; the Transcribe page shows the log of runs started from this window.":
        "Нажмите «Расшифровать снова», чтобы повторить; на странице «Расшифровать файл» виден журнал запусков "
        "из этого окна.",
    "The transcript files were moved or deleted": "Файлы расшифровки перемещены или удалены",
    "(npu-transcribe recorded no output files)": "(npu-transcribe не записал ни одного файла)",
    "Remove from list": "Убрать из списка",
    "Remove “{name}” from the list?": "Убрать «{name}» из списка?",
    "Only the list entry is removed: the recording and its transcripts stay where they are, unless you tick "
    "the box.":
        "Убирается только строка списка: запись и ее расшифровки остаются на месте, если не поставить флажок.",
    "Also move the transcript files to the Trash: {names}": "Также переместить файлы расшифровки в Корзину: {names}",
    "Remove": "Убрать",
    "Could not update the list": "Не удалось обновить список",
    "Removed “{name}” from the list; its transcripts are in the Trash": "«{name}» убран из списка; расшифровки в Корзине",
    "Removed “{name}” from the list": "«{name}» убран из списка",
    # -- the window: Transcribe a file
    "Automatic": "Автоматически",
    "GigaAM v3 (Russian)": "GigaAM v3 (русский)",
    "auto": "авто",
    "English": "английский",
    "Starting…": "Запуск…",
    "Ready": "Ждет запуска",
    "Waiting": "В очереди",
    "Cancelled": "Отменено",
    "Failed: {error}": "Ошибка: {error}",
    "npu-transcribe was not found ({path}). Run the BMeet installer again to set it up.":
        "npu-transcribe не найден ({path}). Запустите установщик BMeet снова, чтобы его установить.",
    "The speech model. Whisper turbo is more accurate and on the NPU about as fast; small needs less memory "
    "and is faster on the CPU.":
        "Модель распознавания речи. Whisper turbo точнее и на NPU почти так же быстр; small занимает меньше "
        "памяти и быстрее на CPU.",
    "Spoken language. Auto-detection listens to the first ~2 minutes.":
        "Язык речи. Автоопределение слушает первые ~2 минуты.",
    "Translate to English": "Перевести на английский",
    "Write English text whatever the spoken language": "Писать текст на английском, на каком бы языке ни говорили",
    "Engine:": "Движок:",
    "Language:": "Язык:",
    "File": "Файл",
    "Options": "Параметры",
    "Transcribe": "Расшифровать",
    "Clear finished": "Убрать завершенные",
    "Show transcript": "Показать расшифровку",
    "Stop this transcription (nothing is written for it)": "Остановить эту расшифровку (для нее ничего не записывается)",
    "Log": "Журнал",
    "npu-transcribe's messages for the selected file appear here.":
        "Здесь появляются сообщения npu-transcribe для выбранного файла.",
    "Detect automatically": "Определить автоматически",
    "Translation works best with the small model (turbo was not trained to translate).":
        "Перевод лучше всего работает с моделью small (turbo не обучали переводить).",
    "Translation uses the small model (turbo was not trained to translate).":
        "Переводит модель small (turbo не обучали переводить).",
    "GigaAM v3 transcribes the Russian speech, and Whisper turbo redoes the sentences with English terms in them.":
        "GigaAM v3 расшифровывает русскую речь, а предложения с английскими терминами заново расшифровывает "
        "Whisper turbo.",
    "Automatic: GigaAM v3 for Russian, with Whisper turbo redoing the sentences with English terms in them; "
    "Whisper turbo for other languages.":
        "Автоматически: GigaAM v3 для русского (предложения с английскими терминами заново расшифровывает "
        "Whisper turbo), Whisper turbo для других языков.",
    "Automatic: Whisper turbo (GigaAM v3 for Russian is not installed).":
        "Автоматически: Whisper turbo (GigaAM v3 для русского не установлен).",
    "Transcripts are saved next to each file: NAME.srt (subtitles) and NAME.md (notes).":
        "Расшифровки сохраняются рядом с каждым файлом: NAME.srt (субтитры) и NAME.md (заметки).",
    "Choose audio or video files": "Выберите аудио- или видеофайлы",
    "{name} (not found)": "{name} (не найден)",
    "{name} (not audio or video)": "{name} (не аудио и не видео)",
    "{name} (already in the queue)": "{name} (уже в очереди)",
    "Added {files}: choose the options and press Transcribe":
        "Добавлено: {files}. Выберите параметры и нажмите «Расшифровать»",
    "{n} file": ("{n} файл", "{n} файла", "{n} файлов"),
    "(skipped: {names})": "(пропущены: {names})",
    "Not added: {names}": "Не добавлены: {names}",
    "Preparing the model (the first NPU run compiles it, up to a few minutes)…":
        "Подготовка модели (при первом запуске на NPU она компилируется, до нескольких минут)…",
    "Loading the model…": "Загрузка модели…",
    "Transcribing…": "Расшифровка…",
    "could not start {program}": "не удалось запустить {program}",
    "crashed": "аварийное завершение",
    "exit status {code}": "код завершения {code}",
    "Cancelling…": "Отмена…",
    "Transcribing {name}": "Расшифровка: {name}",
    "(+{n} waiting)": "(+{n} в очереди)",
    "{time} elapsed": "прошло {time}",
    "about {time} left": "осталось около {time}",
    "Transcribe {files}": "Расшифровать {files}",
    "Transcribe…": "Расшифровать…",
    "Choose files to transcribe": "Выбрать файлы для расшифровки",
    "Start with the options above": "Начать с параметрами выше",
    # -- the window: banners, the control bar, messages
    "{engine} is not running: dictation and meeting recording are unavailable.":
        "{engine} не запущен: диктовка и запись встреч недоступны.",
    "Start {engine}": "Запустить {engine}",
    "Live status is unavailable: {error}": "Текущее состояние недоступно: {error}",
    "What's New": "Что нового",
    "Update…": "Обновить…",
    "Restart": "Перезапустить",
    "Later": "Позже",
    "Search titles and text…": "Поиск по тексту…",   # (the sidebar is narrow; the tooltip says more)
    "Filter meetings and transcribed files by title and transcript text (Ctrl+F)":
        "Отбирать встречи и расшифрованные файлы по названию и тексту расшифровки (Ctrl+F)",
    "Filter meetings and transcribed files by title and transcript text (⌘F)":
        "Отбирать встречи и расшифрованные файлы по названию и тексту расшифровки (⌘F)",
    "Controls": "Управление",
    "Ready to dictate": "Готов к диктовке",
    "Listening…": "Слушаю…",
    "Status unavailable": "Состояние недоступно",
    "{engine} is off": "{engine} выключен",
    "Dictate to clipboard": "Диктовать в буфер обмена",
    "Discard the current dictation": "Отменить текущую диктовку",
    "Saving transcript…": "Сохранение расшифровки…",
    "Start meeting": "Начать встречу",
    "Record the microphone + system audio and transcribe it as people talk; select the meeting to follow "
    "along (also Meta+Alt+M)":
        "Записывать микрофон и звук системы и расшифровывать по ходу разговора; выберите встречу, чтобы "
        "следить за ней (также Meta+Alt+M)",
    "Record the microphone + system audio and transcribe it as people talk; select the meeting to follow "
    "along (also ⌃⌥M)":
        "Записывать микрофон и звук системы и расшифровывать по ходу разговора; выберите встречу, чтобы "
        "следить за ней (также ⌃⌥M)",
    "In person": "Очная встреча",
    "Start an in-person meeting: record only the microphone and tell the voices in the room apart (also "
    "Meta+Alt+Shift+M)":
        "Начать очную встречу: записывать только микрофон и различать голоса в комнате (также Meta+Alt+Shift+M)",
    "Start an in-person meeting: record only the microphone and tell the voices in the room apart (also "
    "⌃⌥⇧M)":
        "Начать очную встречу: записывать только микрофон и различать голоса в комнате (также ⌃⌥⇧M)",
    "Pause": "Пауза",
    "Stop": "Остановить",
    "Stop the meeting: the last chunk is transcribed and a Markdown copy is saved to {folder}":
        "Остановить встречу: последний фрагмент расшифровывается, и копия Markdown сохраняется в {folder}",
    "Dictation: {state} · model {model} on {device}": "Диктовка: {state} · модель {model} на {device}",
    "idle": "ожидание",
    "recording": "запись",
    "transcribing": "расшифровка",
    "off": "выключена",
    "Dictated text copied to the clipboard: paste it with Ctrl+V":
        "Надиктованный текст скопирован в буфер обмена: вставьте его с помощью Ctrl+V",
    "Dictated text copied to the clipboard: paste it with ⌘V":
        "Надиктованный текст скопирован в буфер обмена: вставьте его с помощью ⌘V",
    "Stop dictation": "Остановить диктовку",
    "Stop listening; the text is transcribed and copied to the clipboard":
        "Перестать слушать; текст расшифровывается и копируется в буфер обмена",
    "Listen until you click again, then copy the text to the clipboard (paste with Ctrl+V).\nTo type straight "
    "into another app at its cursor, press Meta+H in that app (Meta+Alt+H dictates to the clipboard from "
    "anywhere).":
        "Слушать, пока вы не нажмете еще раз, затем скопировать текст в буфер обмена (вставка: Ctrl+V).\n"
        "Чтобы печатать прямо в другом приложении у курсора, нажмите в нем Meta+H (Meta+Alt+H диктует в буфер "
        "обмена откуда угодно).",
    "Listen until you click again, then copy the text to the clipboard (paste with ⌘V).\nTo type straight "
    "into another app at its cursor, press ⌃⌥H in that app (⌃⌥⌘H dictates to the clipboard from "
    "anywhere).":
        "Слушать, пока вы не нажмете еще раз, затем скопировать текст в буфер обмена (вставка: ⌘V).\n"
        "Чтобы печатать прямо в другом приложении у курсора, нажмите в нем ⌃⌥H (⌃⌥⌘H диктует в буфер "
        "обмена откуда угодно).",
    "Meeting saved": "Встреча сохранена",
    "Meeting {time}": "Встреча {time}",
    "Paused {time}": "Пауза {time}",
    "No meeting": "Нет встречи",
    "Resume": "Продолжить",
    "Continue recording": "Продолжить запись",
    "Pause recording (nothing is recorded until you resume)":
        "Приостановить запись (до продолжения ничего не записывается)",
    "Speakers told apart: the meeting transcript was updated": "Собеседники различены: расшифровка встречи обновлена",
    "Stop the meeting to switch the language": "Остановите встречу, чтобы сменить язык",
    "Wait for the dictation to finish to switch the language": "Дождитесь конца диктовки, чтобы сменить язык",
    "Dictation language: {language}. To switch here, turn on more dictation languages in BMeet's settings.":
        "Язык диктовки: {language}. Чтобы переключать его здесь, включите больше языков диктовки в настройках "
        "BMeet.",
    "Dictation language: {language}. To switch here, list more languages in ~/.config/bmeet/languages (e.g. "
    "\"en de\").":
        "Язык диктовки: {language}. Чтобы переключать его здесь, перечислите больше языков в "
        "~/.config/bmeet/languages (например, \"en de\").",
    "Dictation language: {language}. Click to switch to {other} (from the next dictation; also ⌃⌥⇧H).":
        "Язык диктовки: {language}. Нажмите, чтобы переключить на {other} (со следующей диктовки; также ⌃⌥⇧H).",
    "Dictation language: {language}. Click to switch to {other} (restarts voxtype, ~3 s; also Meta+Shift+H).":
        "Язык диктовки: {language}. Нажмите, чтобы переключить на {other} (voxtype перезапускается, ~3 с; "
        "также Meta+Shift+H).",
    "BMeet {version} is installed. Restart this window to use it (it is still {running}).":
        "BMeet {version} установлен. Перезапустите это окно, чтобы им пользоваться (в нем пока {running}).",
    "BMeet {version} is available (you have {installed}).": "Доступен BMeet {version} (у вас {installed}).",
    "Opens a terminal window that installs it: the installer of the new release, with the options BMeet was "
    "installed with (bmeet-ctl update)":
        "Открывает окно терминала, в котором он устанавливается: установщик нового выпуска с теми параметрами, "
        "с которыми был установлен BMeet (bmeet-ctl update)",
    "The update runs in a terminal window": "Обновление идет в окне терминала",
    "Dictation": "Диктовка",
    "Starting voxtype…": "Запуск voxtype…",
    "Could not start voxtype: {error}": "Не удалось запустить voxtype: {error}",
    "status {code}": "код {code}",
    "Details: journalctl --user -u voxtype": "Подробности: journalctl --user -u voxtype",
    "{what} failed: {error}": "{what}: не удалось ({error})",
    "Transcribed {name}": "Расшифрован {name}",
    "Transcription finished": "Расшифровка готова",
    "Transcription of {name} failed: {error}": "Не удалось расшифровать {name}: {error}",
    "Not a recording or a transcript from the lists: {path}": "Это не запись и не расшифровка из списков: {path}",
    "File transcription is turned off: not added {names}": "Расшифровка файлов выключена: не добавлены {names}",
    "Meeting {id} not found": "Встреча {id} не найдена",
    "Transcription running": "Идет расшифровка",
    "A transcription is still running. Quit and cancel it?": "Расшифровка еще идет. Выйти и отменить ее?",
    # -- the window's menus on a Mac
    "File": "Файл",
    "Transcribe Files…": "Расшифровать файлы…",
    "Close Window": "Закрыть окно",
    "About BMeet": "О программе BMeet",
    "Settings…": "Настройки…",
    "Quit BMeet Window": "Завершить окно BMeet",
    "Edit": "Правка",
    "Find": "Найти",
    "View": "Вид",
    "Transcribed Files": "Расшифрованные файлы",
    "Transcribe a File": "Расшифровать файл",
    "Window": "Окно",
    "Minimize": "Свернуть",
    "Zoom": "Изменить масштаб",
    "Help": "Справка",
    "BMeet on a Mac": "BMeet на Mac",
    "BMeet on GitHub": "BMeet на GitHub",
    # -- macos/bmeet-meeting's notifications
    "Meeting NOT started": "Встреча НЕ начата",
    "BMeet could not make the RAM disk for the meeting audio": "BMeet не удалось создать RAM-диск для звука встречи",
    "The meeting audio would not be in RAM": "Звук встречи оказался бы не в оперативной памяти",
    "The meeting recorder did not start": "Запись встречи не запустилась",
    "In-person meeting started": "Очная встреча началась",
    "Microphone only; the voices are told apart when you stop.":
        "Только микрофон; голоса различаются, когда вы остановите встречу.",
    "Meeting recording started": "Запись встречи началась",
    "Microphone + system audio. Press ⌃⌥M again to stop.": "Микрофон и звук системы. Нажмите ⌃⌥M еще раз, чтобы остановить.",
    "(not in {folder}: {why})": "(не в {folder}: {why})",
    "Meeting transcript saved": "Расшифровка встречи сохранена",
    "{file}\nTelling the speakers apart; the file is updated in a few minutes.":
        "{file}\nРазличаем собеседников; файл обновится через несколько минут.",
    "Meeting stopped": "Встреча остановлена",
    "Telling the speakers apart; the transcript goes to {file} in a few minutes.":
        "Различаем собеседников; через несколько минут расшифровка будет в {file}.",
    "Meeting transcript NOT saved": "Расшифровка встречи НЕ сохранена",
    "BMeet could not write in {folder}: {why}": "BMeet не удалось записать в {folder}: {why}",
    "Nothing was transcribed": "Ничего не расшифровано",
    "Speakers not told apart": "Собеседники не различены",
    "Could not start npu-transcribe; the transcript has You/Remote only":
        "Не удалось запустить npu-transcribe; в расшифровке только You/Remote",
    "Meeting recording stopped unexpectedly": "Запись встречи неожиданно остановилась",
    "BMeet stops the meeting and saves what was recorded. Start a new one to go on.":
        "BMeet останавливает встречу и сохраняет записанное. Чтобы продолжить, начните новую.",
    "Meeting start or stop already running": "Встреча уже начинается или останавливается",
    "Press again in a moment if you meant another one.": "Если вы хотели другое, нажмите еще раз чуть позже.",
}
