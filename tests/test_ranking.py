from launchdeck.launcher.models import KIND_APP, KIND_COMMAND, KIND_FILE, KIND_FOLDER, Item
from launchdeck.launcher.ranking import SearchIndex, bounded_distance, normalize


def app(name):
    return Item(KIND_APP, name, rf"C:\Start Menu\{name}.lnk")


def file(name, folder=r"C:\Users\me\Documents"):
    return Item(KIND_FILE, name, rf"{folder}\{name}")


ITEMS = [
    app("Visual Studio Code"),
    app("Google Chrome"),
    app("Windows PowerShell"),
    app("Notepad"),
    app("Notepad++"),
    file("notes.txt"),
    file("Quarterly_Report-2024.pdf"),
    file("annual report.docx"),
    Item(KIND_FOLDER, "Projects", r"C:\Users\me\Projects"),
    file("café menu.docx"),
]


def names(results):
    return [r.item.name for r in results]


def test_exact_match_ranks_first():
    res = SearchIndex(ITEMS).search("notepad")
    assert res[0].item.name == "Notepad"
    assert res[0].match == "exact"
    # The prefix match for Notepad++ still appears, below the exact match.
    assert "Notepad++" in names(res)
    assert res[0].score > res[1].score


def test_prefix_beats_substring():
    items = ITEMS + [file("keynote slides.pptx"), file("denote.txt")]
    res = SearchIndex(items).search("note")
    by_name = {r.item.name: r for r in res}
    assert by_name["notes.txt"].match == "prefix"
    assert by_name["denote.txt"].match == "substring"
    assert by_name["notes.txt"].score > by_name["denote.txt"].score
    assert names(res)[0] == "Notepad"  # prefix match plus the app bonus


def test_word_prefix_and_token_matches():
    idx = SearchIndex(ITEMS)
    assert idx.search("code")[0].item.name == "Visual Studio Code"
    assert idx.search("code")[0].match == "word"
    assert idx.search("vis cod")[0].item.name == "Visual Studio Code"
    res = idx.search("report 2024")
    assert res[0].item.name == "Quarterly_Report-2024.pdf"


def test_acronym_match():
    res = SearchIndex(ITEMS).search("vsc")
    assert res[0].item.name == "Visual Studio Code"
    assert res[0].match == "acronym"


def test_substring_match():
    res = SearchIndex(ITEMS).search("arterly")
    assert res and res[0].item.name == "Quarterly_Report-2024.pdf"
    assert res[0].match == "substring"


def test_fuzzy_typos():
    idx = SearchIndex(ITEMS)
    for typo, expected in [("chrme", "Google Chrome"), ("powrshell", "Windows PowerShell"),
                           ("notpead", "Notepad"), ("porjects", "Projects")]:
        res = idx.search(typo)
        assert res, typo
        assert res[0].item.name == expected, (typo, names(res))
        assert res[0].match == "fuzzy"


def test_strong_matches_outrank_fuzzy():
    items = ITEMS + [file("chrome-setup-notes.txt")]
    res = SearchIndex(items).search("chrome")
    assert res[0].item.name == "Google Chrome"
    assert all(r.match != "fuzzy" for r in res)


def test_case_insensitive_and_accents():
    idx = SearchIndex(ITEMS)
    assert names(idx.search("QUARTERLY"))[0] == "Quarterly_Report-2024.pdf"
    assert names(idx.search("NoTePaD"))[0] == "Notepad"
    assert names(idx.search("cafe"))[0] == "café menu.docx"


def test_whitespace_and_punctuation_normalised():
    assert normalize("  Quarterly_Report-2024.pdf ") == "quarterly report 2024 pdf"
    idx = SearchIndex(ITEMS)
    assert names(idx.search("  quarterly   report  "))[0] == "Quarterly_Report-2024.pdf"


def test_no_matches_returns_empty():
    assert SearchIndex(ITEMS).search("zzqxv") == []
    assert SearchIndex(ITEMS).search("   ") == []


def test_duplicates_are_removed():
    dup_app_user = Item(KIND_APP, "Google Chrome", r"C:\Users\me\Start Menu\Google Chrome.lnk")
    dup_app_common = Item(KIND_APP, "Google Chrome", r"C:\ProgramData\Start Menu\Google Chrome.lnk")
    same_file_twice = [file("notes.txt"), Item(KIND_FILE, "notes.txt", r"c:\users\ME\documents\NOTES.TXT")]
    idx = SearchIndex([dup_app_user, dup_app_common] + same_file_twice)
    assert len(idx) == 2
    res = idx.search("chrome")
    assert names(res) == ["Google Chrome"]


def test_keywords_and_kind_bonus():
    cmd = Item(KIND_COMMAND, "Open Notepad with file", "cmd-1", "desc", ("np",))
    idx = SearchIndex(ITEMS + [cmd])
    res = idx.search("np")
    assert res[0].item.key == cmd.key
    assert res[0].match == "exact"


def test_ranking_is_deterministic():
    idx = SearchIndex(ITEMS)
    first = names(idx.search("o"))
    for _ in range(5):
        assert names(SearchIndex(list(reversed(ITEMS))).search("o")) == first


def test_usage_boost_breaks_ties_without_overriding_tiers():
    a = file("plan.txt", r"C:\a")
    b = file("plan.txt", r"C:\b")
    idx = SearchIndex([a, b])
    assert idx.search("plan")[0].item.key == a.key
    boosted = idx.with_usage({b.key: 5})
    assert boosted.search("plan")[0].item.key == b.key
    # A frequently used weak match must not jump above an exact match.
    weak = file("my plans and notes.txt")
    idx2 = SearchIndex([a, weak]).with_usage({weak.key: 1000})
    assert idx2.search("plan.txt")[0].item.key == a.key


def test_bounded_distance():
    assert bounded_distance("chrme", "chrome", 1) == 1
    assert bounded_distance("teh", "the", 1) == 1  # transposition
    assert bounded_distance("abc", "xyz", 1) == 2  # exceeds the limit
    assert bounded_distance("same", "same", 2) == 0


def test_large_index_is_fast():
    import random
    import string
    import time

    random.seed(7)
    words = ["".join(random.choices(string.ascii_lowercase, k=random.randint(3, 9))) for _ in range(5000)]
    items = [Item(KIND_FILE, " ".join(random.choices(words, k=3)) + ".txt", f"C:/x/{i}.txt") for i in range(40000)]
    idx = SearchIndex(items + ITEMS)
    start = time.perf_counter()
    for q in ("notepad", "chrme", "report 2024", "qwrtyu"):
        idx.search(q, 20)
    elapsed = time.perf_counter() - start
    assert elapsed < 1.0, f"4 searches over 40k entries took {elapsed:.2f}s"
