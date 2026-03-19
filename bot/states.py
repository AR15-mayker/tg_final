from aiogram.fsm.state import State, StatesGroup


class ReminderStates(StatesGroup):
    awaiting_time = State()
    awaiting_event_time = State()
    awaiting_event_text = State()
