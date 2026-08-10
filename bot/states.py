from aiogram.fsm.state import State, StatesGroup


class UploadStates(StatesGroup):
    waiting_file = State()


class CloneStates(StatesGroup):
    waiting_link = State()
    choosing_destination = State()


class DriveStates(StatesGroup):
    waiting_mkdir_name = State()
    waiting_rename = State()
    browsing = State()


class AdminStates(StatesGroup):
    waiting_broadcast = State()
    waiting_user_lookup = State()
