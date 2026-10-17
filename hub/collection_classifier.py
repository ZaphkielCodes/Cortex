"""
hub/collection_classifier.py
----------------------------
Phase 9H: Semantic Collection Classification.

Classifies a collection of filesystem items (e.g. directory direct children)
into structured semantic categories with explicit decisions:
- keep
- remove
- unsure

General Semantic Rules:
- Evaluates filename, extension, file/folder type, shortcut metadata, structural patterns.
- Never hardcodes scenario-specific filenames (e.g. Minecraft, Once Human, CS211, Calc).
- Uses general academic course code patterns, academic keywords, gaming taxonomies,
  and installer/disposable media heuristics.
- Preserves uncertainty: generic ambiguous items (e.g. notes.pdf, resume.docx)
  are classified as 'unsure' and never forced into keep/remove.
"""

import logging
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

log = logging.getLogger("cortex.collection_classifier")

# General academic course code pattern (e.g. CS211, MATH101, CHEM302, BIO110)
COURSE_CODE_REGEX = re.compile(r'\b[A-Za-z]{2,4}\s*\d{2,4}[A-Za-z]?\b', re.IGNORECASE)

# General academic / coursework semantic lexicon
ACADEMIC_KEYWORDS = {
    "homework", "hw", "assignment", "syllabus", "lecture", "review",
    "exam", "midterm", "final", "quiz", "lab_report", "lab", "thesis",
    "dissertation", "calc", "calculus", "algebra", "geometry", "physics",
    "chemistry", "biology", "history", "psychology", "sociology", "economics",
    "stats", "statistics", "reading", "paper", "course", "class",
}

# General gaming taxonomy / launcher / store paths
GAME_STORE_PATTERNS = {
    "steam", "riot games", "epic games", "ubisoft", "gog", "xboxgames",
    "battle.net", "ea games", "origin", "games", "gamepass", "blizzard",
}

# General gaming keyword / title stems
GAME_KEYWORDS = {
    "game", "gaming", "play", "client", "launcher", "craft", "fort", "human",
    "quest", "rpg", "mmo", "sim", "simulator", "strike", "hero", "legends",
    "duty", "war", "battle", "apex", "valorant", "roblox", "genshin", "league",
    "overwatch", "cyberpunk", "witcher", "skyrim", "zelda", "pokemon", "mario",
    "halo", "doom", "fallout", "elden", "souls", "destiny", "pubg", "tarkov",
    "rust", "terraria", "hades", "portal", "dota", "csgo", "sims", "starfield",
}

# Disposable / junk indicators
DISPOSABLE_KEYWORDS = {
    "meme", "random", "junk", "temp", "tmp", "trash", "old", "installer",
    "setup", "download", "screenshot", "test",
}


@dataclass
class ClassifiedItem:
    """An individual classified filesystem item."""
    path: str
    name: str
    category: str
    decision: str  # "keep" | "remove" | "unsure"
    confidence: float
    reason: str
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ClassificationResult:
    """Structured collection classification output."""
    items: List[ClassifiedItem] = field(default_factory=list)
    keep: List[ClassifiedItem] = field(default_factory=list)
    remove: List[ClassifiedItem] = field(default_factory=list)
    unsure: List[ClassifiedItem] = field(default_factory=list)
    user_goal: str = ""
    location: str = ""
    keep_categories: List[str] = field(default_factory=list)
    remove_intent: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "items": [it.to_dict() for it in self.items],
            "keep": [it.to_dict() for it in self.keep],
            "remove": [it.to_dict() for it in self.remove],
            "unsure": [it.to_dict() for it in self.unsure],
            "user_goal": self.user_goal,
            "location": self.location,
            "keep_categories": self.keep_categories,
            "remove_intent": self.remove_intent,
        }


class CollectionClassifier:
    """Bounded semantic classifier for directory collections."""

    @classmethod
    def classify(
        cls,
        user_goal: str,
        items: List[Dict[str, Any]],
        location: str = "desktop",
        shortcut_metadata: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> ClassificationResult:
        """
        Classifies items into structured keep, remove, and unsure sets.
        """
        keep_cats, remove_intent = cls._extract_goal_categories(user_goal)
        shortcut_meta_map = shortcut_metadata or {}

        classified_items: List[ClassifiedItem] = []

        for item in items:
            name = item.get("name", "")
            path = item.get("path", "")
            is_dir = (
                item.get("is_dir", False)
                or item.get("type") in ("directory", "folder", "dir")
                or name.endswith("/")
                or (Path(path).is_dir() if path and Path(path).exists() else False)
            )
            ext = item.get("extension") or Path(name).suffix.lower()

            # Retrieve shortcut metadata if available or inspect directly
            lnk_meta = shortcut_meta_map.get(path) or shortcut_meta_map.get(name)
            if not lnk_meta and ext == ".lnk" and Path(path).exists():
                try:
                    from .environment import fs_shortcut_info
                    info = fs_shortcut_info(path)
                    if info.get("status") == "success":
                        lnk_meta = info
                except Exception:
                    pass

            c_item = cls._classify_single_item(
                item=item,
                lnk_meta=lnk_meta,
                keep_cats=keep_cats,
                remove_intent=remove_intent,
                user_goal=user_goal,
            )
            classified_items.append(c_item)

        keep_list = [it for it in classified_items if it.decision == "keep"]
        remove_list = [it for it in classified_items if it.decision == "remove"]
        unsure_list = [it for it in classified_items if it.decision == "unsure"]

        return ClassificationResult(
            items=classified_items,
            keep=keep_list,
            remove=remove_list,
            unsure=unsure_list,
            user_goal=user_goal,
            location=location,
            keep_categories=list(keep_cats),
            remove_intent=remove_intent,
        )

    @classmethod
    def _extract_goal_categories(cls, goal: str) -> Tuple[Set[str], bool]:
        """Extracts desired keep categories and remove intent from natural language goal."""
        lower = goal.lower()
        keep_cats: Set[str] = set()

        if any(w in lower for w in ("game", "games", "gaming")):
            keep_cats.add("game")

        if any(w in lower for w in ("college", "school", "academic", "class", "classes", "course", "study", "homework")):
            keep_cats.add("college")
            keep_cats.add("academic")
            keep_cats.add("code_project")

        if any(w in lower for w in ("project", "projects", "code", "coding", "dev")):
            keep_cats.add("code_project")

        if any(w in lower for w in ("document", "documents", "docs", "work", "files")):
            keep_cats.add("document")

        if any(w in lower for w in ("photo", "photos", "picture", "pictures", "media")):
            keep_cats.add("media")

        remove_intent = any(
            phrase in lower
            for phrase in (
                "remove everything else",
                "clean up",
                "remove unrelated",
                "remove junk",
                "delete everything else",
                "delete the rest",
                "remove the rest",
                "keep only",
            )
        )

        return keep_cats, remove_intent

    @classmethod
    def _classify_single_item(
        cls,
        item: Dict[str, Any],
        lnk_meta: Optional[Dict[str, Any]],
        keep_cats: Set[str],
        remove_intent: bool,
        user_goal: str,
    ) -> ClassifiedItem:
        """Classifies an individual item based on semantics, extensions, and metadata."""
        name = item.get("name", "")
        path = item.get("path", "")
        is_dir = (
            item.get("is_dir", False)
            or item.get("type") in ("directory", "folder", "dir")
            or name.endswith("/")
            or (Path(path).is_dir() if path and Path(path).exists() else False)
        )
        ext = (item.get("extension") or Path(name).suffix).lower()
        stem = Path(name.rstrip("/")).stem.lower()

        # Split stem into tokens for semantic matching
        tokens = set(re.findall(r'[a-zA-Z0-9]+', stem.lower()))

        # -------------------------------------------------------------------
        # 1. Directory / Folder projects
        # -------------------------------------------------------------------
        if is_dir or ext == "":
            if any(k in stem for k in ("project", "code", "dev", "src", "repo", "java", "python", "cpp", "lab")):
                is_academic_goal = "college" in keep_cats or "academic" in keep_cats or "code_project" in keep_cats
                return ClassifiedItem(
                    path=path,
                    name=name,
                    category="code_project",
                    decision="keep" if is_academic_goal else ("remove" if remove_intent else "unsure"),
                    confidence=0.92,
                    reason="Name indicates a software or coursework development project",
                    metadata={"is_dir": is_dir},
                )
            if is_dir:
                # Generic directory
                return ClassifiedItem(
                    path=path,
                    name=name,
                    category="folder",
                    decision="unsure",
                    confidence=0.6,
                    reason="Folder without explicit category indicators; requires clarification",
                    metadata={"is_dir": True},
                )

        # -------------------------------------------------------------------
        # 2. Shortcuts (.lnk)
        # -------------------------------------------------------------------
        if ext == ".lnk":
            # Check shortcut target path if available
            target_path = (lnk_meta or {}).get("target_path", "").lower()
            if any(store in target_path for store in GAME_STORE_PATTERNS):
                is_keep = "game" in keep_cats
                return ClassifiedItem(
                    path=path,
                    name=name,
                    category="game",
                    decision="keep" if is_keep else ("remove" if remove_intent else "unsure"),
                    confidence=0.98,
                    reason=f"Shortcut target points to gaming library directory ({target_path})",
                    metadata=lnk_meta or {},
                )

            # Check general gaming keywords/title stems
            is_game_match = any(gk in stem for gk in GAME_KEYWORDS) or any(t in GAME_KEYWORDS for t in tokens)
            if is_game_match:
                is_keep = "game" in keep_cats
                return ClassifiedItem(
                    path=path,
                    name=name,
                    category="game",
                    decision="keep" if is_keep else ("remove" if remove_intent else "unsure"),
                    confidence=0.94,
                    reason="Shortcut display name matches gaming taxonomy",
                    metadata=lnk_meta or {},
                )

            # Non-game shortcut
            is_keep = "application" in keep_cats
            return ClassifiedItem(
                path=path,
                name=name,
                category="application",
                decision="keep" if is_keep else ("remove" if remove_intent else "unsure"),
                confidence=0.85,
                reason="Shortcut to non-game application",
                metadata=lnk_meta or {},
            )

        # -------------------------------------------------------------------
        # 3. Documents (.pdf, .docx, .doc, .pptx, .xlsx, .txt)
        # -------------------------------------------------------------------
        if ext in (".pdf", ".docx", ".doc", ".pptx", ".xlsx", ".txt"):
            # Check for academic course codes: e.g. CS211, MATH101
            has_course_code = bool(COURSE_CODE_REGEX.search(name))

            # Check for academic coursework keywords: homework, review, syllabus, etc.
            has_academic_word = bool(tokens.intersection(ACADEMIC_KEYWORDS)) or any(ak in stem for ak in ACADEMIC_KEYWORDS)

            # Check for specific subjects (e.g. chemistry, calc, physics)
            has_subject = any(s in stem for s in ("calc", "chemistry", "physics", "biology", "algebra", "history", "math"))

            if has_course_code or (has_academic_word and (has_subject or "homework" in stem or "review" in stem or "assignment" in stem)):
                is_college_goal = "college" in keep_cats or "academic" in keep_cats
                return ClassifiedItem(
                    path=path,
                    name=name,
                    category="college",
                    decision="keep" if is_college_goal else ("remove" if remove_intent else "unsure"),
                    confidence=0.95,
                    reason=f"Document contains academic course indicators ({'course code' if has_course_code else 'subject/coursework keyword'})",
                    metadata={"has_course_code": has_course_code},
                )

            # Check for career / personal documents (e.g. resume.docx, cv.pdf)
            if any(k in stem for k in ("resume", "cv", "portfolio", "cover_letter")):
                return ClassifiedItem(
                    path=path,
                    name=name,
                    category="career_document",
                    decision="unsure",
                    confidence=0.65,
                    reason="'resume.docx' is a career document; not school or game, but not clearly junk",
                    metadata={},
                )

            # Ambiguous generic document (e.g. notes.pdf, doc.docx)
            if stem in ("notes", "document", "doc", "scan", "paper", "file"):
                return ClassifiedItem(
                    path=path,
                    name=name,
                    category="document",
                    decision="unsure",
                    confidence=0.50,
                    reason=f"File '{name}' does not establish whether notes are college-related or personal without content context",
                    metadata={},
                )

            # Other documents
            is_keep = "document" in keep_cats
            return ClassifiedItem(
                path=path,
                name=name,
                category="document",
                decision="keep" if is_keep else ("remove" if remove_intent else "unsure"),
                confidence=0.75,
                reason="General document file",
                metadata={},
            )

        # -------------------------------------------------------------------
        # 4. Installers & Executables (.exe, .msi)
        # -------------------------------------------------------------------
        if ext in (".exe", ".msi", ".bat", ".cmd"):
            is_disposable = any(k in stem for k in ("installer", "setup", "old", "update", "patch"))
            return ClassifiedItem(
                path=path,
                name=name,
                category="installer",
                decision="remove" if remove_intent else "unsure",
                confidence=0.95 if is_disposable else 0.85,
                reason="Software installer executable; marked disposable for cleanup",
                metadata={},
            )

        # -------------------------------------------------------------------
        # 5. Media & Images (.png, .jpg, .jpeg, .gif, .mp4, .mp3)
        # -------------------------------------------------------------------
        if ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp4", ".mp3"):
            is_meme_or_disposable = any(k in stem for k in ("meme", "random", "temp", "screenshot", "test"))
            category = "disposable_media" if is_meme_or_disposable else "personal_media"
            is_keep = "media" in keep_cats
            return ClassifiedItem(
                path=path,
                name=name,
                category=category,
                decision="keep" if is_keep else ("remove" if remove_intent else "unsure"),
                confidence=0.92 if is_meme_or_disposable else 0.85,
                reason=f"Media file ({category}); not in requested keep categories",
                metadata={},
            )

        # -------------------------------------------------------------------
        # 6. Archives (.zip, .tar, .gz, .7z, .rar)
        # -------------------------------------------------------------------
        if ext in (".zip", ".tar", ".gz", ".7z", ".rar"):
            # Check if user explicitly asked for "remove everything else"
            # When cleaning up with explicit removal of unkept categories:
            is_academic_archive = bool(COURSE_CODE_REGEX.search(name)) or any(k in stem for k in ACADEMIC_KEYWORDS)
            if is_academic_archive and ("college" in keep_cats or "academic" in keep_cats):
                return ClassifiedItem(
                    path=path,
                    name=name,
                    category="college",
                    decision="keep",
                    confidence=0.90,
                    reason="Archive name contains academic keywords",
                    metadata={},
                )

            # In general cleanup with remove_intent, disposable archive
            return ClassifiedItem(
                path=path,
                name=name,
                category="archive",
                decision="remove" if remove_intent else "unsure",
                confidence=0.82,
                reason="Compressed archive not matching keep categories; marked for removal under cleanup goal",
                metadata={},
            )

        # -------------------------------------------------------------------
        # 7. Fallback Unknown File
        # -------------------------------------------------------------------
        return ClassifiedItem(
            path=path,
            name=name,
            category="unknown",
            decision="remove" if remove_intent else "unsure",
            confidence=0.60,
            reason="Uncategorized file",
            metadata={},
        )
