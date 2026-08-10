from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder


def owner_keyboard() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="👤 Contact ", url="https://t.me/Dreamm_ca"))
    return b.as_markup()


def login_keyboard(auth_url: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="🔐 Login with Google", url=auth_url))
    return b.as_markup()


def logout_confirm() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="✅ Logout", callback_data="auth:logout_confirm"),
        InlineKeyboardButton(text="❌ Cancel", callback_data="auth:logout_cancel"),
    )
    return b.as_markup()


def clone_confirm(job_key: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="🚀 Clone", callback_data=f"clone:go:{job_key}"))
    b.row(InlineKeyboardButton(text="📁 Select Destination", callback_data=f"clone:dest:{job_key}"))
    b.row(InlineKeyboardButton(text="❌ Cancel", callback_data=f"clone:cancel:{job_key}"))
    return b.as_markup()


def duplicate_confirm(job_id: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="♻️ Use Existing", callback_data=f"dup:use:{job_id}"))
    b.row(InlineKeyboardButton(text="📤 Upload Anyway", callback_data=f"dup:upload:{job_id}"))
    b.row(InlineKeyboardButton(text="❌ Cancel", callback_data=f"dup:cancel:{job_id}"))
    return b.as_markup()


def help_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="☁️ My Drive", callback_data="menu:drive"),
        InlineKeyboardButton(text="👤 My Account", callback_data="menu:account"),
    )
    return b.as_markup()


def drive_browser(
    items: list,
    folder_id: str,
    can_go_back: bool = False,
    can_go_forward: bool = False,
) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for item in items:
        icon = "📁" if item["mimeType"] == "application/vnd.google-apps.folder" else "📄"
        b.row(InlineKeyboardButton(text=f"{icon} {item['name']}", callback_data=f"drive:open:{item['id']}"))
    nav = []
    if can_go_back:
        nav.append(InlineKeyboardButton(text="⬅️ Back", callback_data="drive:back"))
    if can_go_forward:
        nav.append(InlineKeyboardButton(text="➡️ Forward", callback_data="drive:forward"))
    nav.append(InlineKeyboardButton(text="➕ New Folder", callback_data=f"drive:mkdir:{folder_id}"))
    b.row(*nav)
    if folder_id != "root":
        b.row(InlineKeyboardButton(text="🔒 Share This Folder", callback_data=f"drive:share:{folder_id}"))
    return b.as_markup()


def file_actions(file_id: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="✏️ Rename", callback_data=f"drive:rename:{file_id}"),
        InlineKeyboardButton(text="🔒 Sharing", callback_data=f"drive:share:{file_id}"),
    )
    b.row(InlineKeyboardButton(text="🔗 Open Link", callback_data=f"drive:link:{file_id}"))
    b.row(InlineKeyboardButton(text="🗑️ Delete", callback_data=f"drive:delete_confirm:{file_id}"))
    return b.as_markup()


def share_menu(file_id: str, status: dict) -> InlineKeyboardMarkup:
    """status = {'access': 'anyone'|'restricted', 'role': 'reader'|'commenter'|'writer'|None}"""
    access, role = status["access"], status.get("role")
    b = InlineKeyboardBuilder()

    anyone_mark = "✅ " if access == "anyone" else ""
    restricted_mark = "✅ " if access == "restricted" else ""
    b.row(
        InlineKeyboardButton(text=f"{anyone_mark}🌐 Anyone with link", callback_data=f"drive:share_type:anyone:{file_id}"),
        InlineKeyboardButton(text=f"{restricted_mark}🔒 Restricted", callback_data=f"drive:share_type:restricted:{file_id}"),
    )

    if access == "anyone":
        def mark(r):
            return "✅ " if role == r else ""
        b.row(
            InlineKeyboardButton(text=f"{mark('reader')}👁️ Viewer", callback_data=f"drive:share_role:reader:{file_id}"),
            InlineKeyboardButton(text=f"{mark('commenter')}💬 Commenter", callback_data=f"drive:share_role:commenter:{file_id}"),
            InlineKeyboardButton(text=f"{mark('writer')}✏️ Editor", callback_data=f"drive:share_role:writer:{file_id}"),
        )

    b.row(InlineKeyboardButton(text="⬅️ Back", callback_data=f"drive:open:{file_id}"))
    return b.as_markup()


def delete_confirm(file_id: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="🗑️ Move to Trash", callback_data=f"drive:delete:{file_id}"),
        InlineKeyboardButton(text="❌ Cancel", callback_data=f"drive:cancel:{file_id}"),
    )
    return b.as_markup()
