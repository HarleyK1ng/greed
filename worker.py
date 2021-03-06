import datetime
import logging
import os
import queue as queuem
import re
import sys
import threading
import traceback
import uuid
from html import escape
from typing import *

import requests
import sqlalchemy.orm
import telegram
from telegram import CallbackQuery

import database as db
import localization
import nuconfig

log = logging.getLogger(__name__)


class StopSignal:
    """A data class that should be sent to the worker when the conversation has to be stopped abnormally."""

    def __init__(self, reason: str = ""):
        self.reason = reason


class CancelSignal:
    """An empty class that is added to the queue whenever the user presses a cancel inline button."""
    pass


def replace_digits_to_emoji(text: str = None):
    replacements = {'0': '0️⃣', '1': '1️⃣', '2': '2️⃣', '3': '3️⃣', '4': '4️⃣', '5': '5️⃣', '6': '6️⃣', '7': '7️⃣',
                    '8': '8️⃣', '9': '9️⃣'}
    text = "".join([replacements.get(c, c) for c in text])
    return text


class Worker(threading.Thread):
    """A worker for a single conversation. A new one is created every time the /start command is sent."""

    def __init__(self,
                 bot,
                 chat: telegram.Chat,
                 telegram_user: telegram.User,
                 cfg: nuconfig.NuConfig,
                 engine,
                 *args,
                 **kwargs):
        # Initialize the thread
        super().__init__(name=f"Worker {chat.id}", *args, **kwargs)
        # Store the bot, chat info and config inside the class
        self.bot = bot
        self.chat: telegram.Chat = chat
        self.telegram_user: telegram.User = telegram_user
        self.cfg = cfg
        self.loc = None
        # Open a new database session
        log.debug(f"Opening new database session for {self.name}")
        self.session = sqlalchemy.orm.sessionmaker(bind=engine)()
        # Get the user db data from the users and admin tables
        self.user: Optional[db.User] = None
        self.admin: Optional[db.Admin] = None
        # The sending pipe is stored in the Worker class, allowing the forwarding of messages to the chat process
        self.queue = queuem.Queue()
        # # The current active invoice payload; reject all invoices with a different payload
        # self.invoice_payload = None
        # The price class of this worker.
        self.Price = self.price_factory()

    def __repr__(self):
        return f"<{self.__class__.__qualname__} {self.chat.id}>"

    # noinspection PyMethodParameters
    def price_factory(worker):
        class Price:
            """The base class for the prices in greed.
            Its int value is in minimum units, while its float and str values are in decimal format."""

            def __init__(self, value: Union[int, float, str, "Price"]):
                if isinstance(value, int):
                    # Keep the value as it is
                    self.value = int(value)
                elif isinstance(value, float):
                    # Convert the value to minimum units
                    self.value = int(value * (10 ** worker.cfg["Payments"]["currency_exp"]))
                elif isinstance(value, str):
                    # Remove decimal points, then cast to int
                    self.value = int(float(value.replace(",", ".")) * (10 ** worker.cfg["Payments"]["currency_exp"]))
                elif isinstance(value, Price):
                    # Copy self
                    self.value = value.value

            def __repr__(self):
                return f"<{self.__class__.__qualname__} of value {self.value}>"

            def __str__(self):
                return worker.loc.get(
                    "currency_format_string",
                    symbol=worker.cfg["Payments"]["currency_symbol"],
                    value="{0:.2f}".format(self.value / (10 ** worker.cfg["Payments"]["currency_exp"]))
                )

            def __int__(self):
                return self.value

            def __float__(self):
                return self.value / (10 ** worker.cfg["Payments"]["currency_exp"])

            def __ge__(self, other):
                return self.value >= Price(other).value

            def __le__(self, other):
                return self.value <= Price(other).value

            def __eq__(self, other):
                return self.value == Price(other).value

            def __gt__(self, other):
                return self.value > Price(other).value

            def __lt__(self, other):
                return self.value < Price(other).value

            def __add__(self, other):
                return Price(self.value + Price(other).value)

            def __sub__(self, other):
                return Price(self.value - Price(other).value)

            def __mul__(self, other):
                return Price(int(self.value * other))

            def __floordiv__(self, other):
                return Price(int(self.value // other))

            def __radd__(self, other):
                return self.__add__(other)

            def __rsub__(self, other):
                return Price(Price(other).value - self.value)

            def __rmul__(self, other):
                return self.__mul__(other)

            def __iadd__(self, other):
                self.value += Price(other).value
                return self

            def __isub__(self, other):
                self.value -= Price(other).value
                return self

            def __imul__(self, other):
                self.value *= other
                self.value = int(self.value)
                return self

            def __ifloordiv__(self, other):
                self.value //= other
                return self

        return Price

    def run(self):
        """The conversation code."""
        log.debug("Starting conversation")
        # Get the user db data from the users and admin tables
        self.user = self.session.query(db.User).filter(db.User.user_id == self.chat.id).one_or_none()
        self.admin = self.session.query(db.Admin).filter(db.Admin.user_id == self.chat.id).one_or_none()
        # If the user isn't registered, create a new record and add it to the db
        if self.user is None:
            # Check if there are other registered users: if there aren't any, the first user will be owner of the bot
            will_be_owner = (self.session.query(db.Admin).first() is None)
            # Create the new record
            self.user = db.User(w=self)
            # Add the new record to the db
            self.session.add(self.user)
            # Flush the session to get an userid
            self.session.flush()
            # If the will be owner flag is set
            if will_be_owner:
                # Become owner
                self.admin = db.Admin(user_id=self.user.user_id,
                                      edit_products=True,
                                      display_on_help=True,
                                      is_owner=True,
                                      live_mode=False)
                # Add the admin to the transaction
                self.session.add(self.admin)
            # Commit the transaction
            self.session.commit()
            log.info(f"Created new user: {self.user}")
            if will_be_owner:
                log.warning(f"User was auto-promoted to Admin as no other admins existed: {self.user}")
        # Create the localization object
        self.__create_localization()
        # Capture exceptions that occour during the conversation
        # noinspection PyBroadException
        try:
            # Welcome the user to the bot
            if self.cfg["Appearance"]["display_welcome_message"] == "yes":
                self.bot.send_message(self.chat.id, self.loc.get("conversation_after_start"))
            # If the user is not an admin, send him to the user menu
            if self.admin is None:
                self.__user_menu()
            # If the user is an admin, send him to the admin menu
            else:
                # Clear the live orders flag
                self.admin.live_mode = False
                # Commit the change
                self.session.commit()
                # Open the admin menu
                self.__admin_menu()
        except Exception as e:
            # Try to notify the user of the exception
            # noinspection PyBroadException
            try:
                self.bot.send_message(self.chat.id, self.loc.get("fatal_conversation_exception"))
            except Exception as ne:
                log.error(f"Failed to notify the user of a conversation exception: {ne}")
            log.error(f"Exception in {self}: {e}")
            traceback.print_exception(*sys.exc_info())

    def is_ready(self):
        # Change this if more parameters are added!
        return self.loc is not None

    def stop(self, reason: str = ""):
        """Gracefully stop the worker process"""
        # Send a stop message to the thread
        self.queue.put(StopSignal(reason))
        # Wait for the thread to stop
        self.join()

    def update_user(self) -> db.User:
        """Update the user data."""
        log.debug("Fetching updated user data from the database")
        self.user = self.session.query(db.User).filter(db.User.user_id == self.chat.id).one_or_none()
        return self.user

    # noinspection PyUnboundLocalVariable
    def __receive_next_update(self) -> telegram.Update:
        """Get the next update from the queue.
        If no update is found, block the process until one is received.
        If a stop signal is sent, try to gracefully stop the thread."""
        # Pop data from the queue
        try:
            data = self.queue.get(timeout=self.cfg["Telegram"]["conversation_timeout"])
        except queuem.Empty:
            # If the conversation times out, gracefully stop the thread
            self.__graceful_stop(StopSignal("timeout"))
        # Check if the data is a stop signal instance
        if isinstance(data, StopSignal):
            # Gracefully stop the process
            self.__graceful_stop(data)
        # Return the received update
        return data

    def __wait_for_specific_message(self,
                                    items: List[str],
                                    cancellable: bool = False) -> Union[str, CancelSignal]:
        """Continue getting updates until until one of the strings contained in the list is received as a message."""
        log.debug("Waiting for a specific message...")
        while True:
            # Get the next update
            update = self.__receive_next_update()
            # If a CancelSignal is received...
            if isinstance(update, CancelSignal):
                # And the wait is cancellable...
                if cancellable:
                    # Return the CancelSignal
                    return update
                else:
                    # Ignore the signal
                    continue
            # Ensure the update contains a message
            if update.message is None:
                continue
            # Ensure the message contains text
            if update.message.text is None:
                continue
            # Check if the message is contained in the list
            if update.message.text not in items:
                continue
            # Return the message text
            return update.message.text

    def __wait_for_regex(self, regex: str, cancellable: bool = False) -> Union[str, CancelSignal]:
        """Continue getting updates until the regex finds a match in a message, then return the first capture group."""
        log.debug("Waiting for a regex...")
        while True:
            # Get the next update
            update = self.__receive_next_update()
            # If a CancelSignal is received...
            if isinstance(update, CancelSignal):
                # And the wait is cancellable...
                if cancellable:
                    # Return the CancelSignal
                    return update
                else:
                    # Ignore the signal
                    continue
            # Ensure the update contains a message
            if update.message is None:
                continue
            # Ensure the message contains text
            if update.message.text is None:
                continue
            # Try to match the regex with the received message
            match = re.search(regex, update.message.text)
            # Ensure there is a match
            if match is None:
                continue
            # Return the first capture group
            return match.group(1)

    def __wait_for_sizes(self, regex: str, cancellable: bool = False) -> Union[str, CancelSignal]:
        """Continue getting updates until the regex finds a match in a message, then return the first capture group."""
        log.debug("Waiting for a regex...")
        while True:
            # Get the next update
            update = self.__receive_next_update()
            # If a CancelSignal is received...
            if isinstance(update, CancelSignal):
                # And the wait is cancellable...
                if cancellable:
                    # Return the CancelSignal
                    return update
                else:
                    # Ignore the signal
                    continue
            # Ensure the update contains a message
            if update.message is None:
                continue
            # Ensure the message contains text
            if update.message.text is None:
                continue
            # Try to match the regex with the received message
            match = re.search(regex, update.message.text)
            # Ensure there is a match
            if match is None:
                continue
            # Return the first capture group
            return match.groups()

    def __wait_for_precheckoutquery(self,
                                    cancellable: bool = False) -> Union[telegram.PreCheckoutQuery, CancelSignal]:
        """Continue getting updates until a precheckoutquery is received.
        The payload is checked by the core before forwarding the message."""
        log.debug("Waiting for a PreCheckoutQuery...")
        while True:
            # Get the next update
            update = self.__receive_next_update()
            # If a CancelSignal is received...
            if isinstance(update, CancelSignal):
                # And the wait is cancellable...
                if cancellable:
                    # Return the CancelSignal
                    return update
                else:
                    # Ignore the signal
                    continue
            # Ensure the update contains a precheckoutquery
            if update.pre_checkout_query is None:
                continue
            # Return the precheckoutquery
            return update.pre_checkout_query

    def __wait_for_successfulpayment(self,
                                     cancellable: bool = False) -> Union[telegram.SuccessfulPayment, CancelSignal]:
        """Continue getting updates until a successfulpayment is received."""
        log.debug("Waiting for a SuccessfulPayment...")
        while True:
            # Get the next update
            update = self.__receive_next_update()
            # If a CancelSignal is received...
            if isinstance(update, CancelSignal):
                # And the wait is cancellable...
                if cancellable:
                    # Return the CancelSignal
                    return update
                else:
                    # Ignore the signal
                    continue
            # Ensure the update contains a message
            if update.message is None:
                continue
            # Ensure the message is a successfulpayment
            if update.message.successful_payment is None:
                continue
            # Return the successfulpayment
            return update.message.successful_payment

    def __wait_for_photo(self, cancellable: bool = False) -> Union[List[telegram.PhotoSize], CancelSignal]:
        """Continue getting updates until a photo is received, then return it."""
        log.debug("Waiting for a photo...")
        while True:
            # Get the next update
            update = self.__receive_next_update()
            # If a CancelSignal is received...
            if isinstance(update, CancelSignal):
                # And the wait is cancellable...
                if cancellable:
                    # Return the CancelSignal
                    return update
                else:
                    # Ignore the signal
                    continue
            # Ensure the update contains a message
            if update.message is None:
                continue
            # Ensure the message contains a photo
            if update.message.photo is None:
                continue
            # Return the photo array
            return update.message.photo

    def __wait_for_contact(self, cancellable: bool = False):
        # TODO: Добавить в конфиг настройку регионального формата номеров, чтобы был правильный regex
        regex = r"(([+\(]{0,1}\d{0,3}[ -]{0,1}\({0,1}\d{2}\){0,1}[ -]{0,1}\d{3}[ -]{0,1}[ -]{0,1}\d{2}[ -]{0,1}\d{2}))"
        while True:
            # Get the next update
            update = self.__receive_next_update()
            # If a CancelSignal is received...
            if isinstance(update, CancelSignal):
                # And the wait is cancellable...
                if cancellable:
                    # Return the CancelSignal
                    return update
                else:
                    # Ignore the signal
                    continue
            # Ensure the update contains a message
            if update.message is None:
                continue
            if update.message.contact is not None:
                number = update.message.contact.phone_number
                return number
            if update.message.text is not None:
                match = re.search(regex, update.message.text)
                if match is None:
                    continue
                else:
                    return match.group(1)

    def __wait_for_inlinekeyboard_callback(self, accept_location: bool = False,
                                           accept_text: bool = False, cancellable: bool = False) \
            -> Union[telegram.CallbackQuery, CancelSignal]:
        """Continue getting updates until an inline keyboard callback is received, then return it."""
        log.debug("Waiting for a CallbackQuery...")
        while True:
            # Get the next update
            update = self.__receive_next_update()
            # If a CancelSignal is received...
            if isinstance(update, CancelSignal):
                # And the wait is cancellable...
                if cancellable:
                    # Return the CancelSignal
                    return update
                else:
                    # Ignore the signal
                    continue
            if accept_location is True:
                if update.message is not None:
                    if update.message.location is not None:
                        return update.message
                    pass
            if accept_text is True:
                if update.message is not None:
                    if update.message.text is not None:
                        return update.message
                    pass
            # Ensure the update is a CallbackQuery
            if update.callback_query is None:
                continue
            # Answer the callbackquery
            self.bot.answer_callback_query(update.callback_query.id)
            # Return the callbackquery
            return update.callback_query

    def __user_select(self) -> Union[db.User, CancelSignal]:
        """Select an user from the ones in the database."""
        log.debug("Waiting for a user selection...")
        # Find all the users in the database
        users = self.session.query(db.User).order_by(db.User.user_id).all()
        # Create a list containing all the keyboard button strings
        keyboard_buttons = [[self.loc.get("menu_cancel")]]
        # Add to the list all the users
        for user in users:
            keyboard_buttons.append([user.identifiable_str()])
        # Create the keyboard
        keyboard = telegram.ReplyKeyboardMarkup(keyboard_buttons, one_time_keyboard=True,
                                                resize_keyboard=True)
        # Keep asking until a result is returned
        while True:
            # Send the keyboard
            self.bot.send_message(self.chat.id, self.loc.get("conversation_admin_select_user"), reply_markup=keyboard)
            # Wait for a reply
            reply = self.__wait_for_regex("user_([0-9]+)", cancellable=True)
            # Propagate CancelSignals
            if isinstance(reply, CancelSignal):
                return reply
            # Find the user in the database
            user = self.session.query(db.User).filter_by(user_id=int(reply)).one_or_none()
            # Ensure the user exists
            if not user:
                self.bot.send_message(self.chat.id, self.loc.get("error_user_does_not_exist"))
                continue
            return user

    def __user_menu(self):
        """Function called from the run method when the user is not an administrator.
        Normal bot actions should be placed here."""
        log.debug("Displaying __user_menu")
        # Loop used to returning to the menu after executing a command
        while True:
            # Create a keyboard with the user main menu
            # TODO: Добавить кнопки: Контакты(О нас, Адреса филиалов),
            #  Настройки(язык, номер телефона, Имя), Мои заказы(повторить),
            #  Написать отзыв
            keyboard = [
                [telegram.KeyboardButton(self.loc.get("menu_order"))],
                [telegram.KeyboardButton(self.loc.get("menu_rate"))]
            ]
            # Send the previously created keyboard to the user (ensuring it can be clicked only 1 time)
            self.bot.send_message(self.chat.id,
                                  self.loc.get("conversation_open_user_menu"),
                                  reply_markup=telegram.ReplyKeyboardMarkup(keyboard, one_time_keyboard=True,
                                                                            resize_keyboard=True))
            # Wait for a reply from the user
            selection = self.__wait_for_specific_message([
                self.loc.get("menu_order"),
                self.loc.get("menu_rate"),
            ])
            # After the user reply, update the user data
            self.update_user()
            # If the user has selected the Order option...
            if selection == self.loc.get("menu_order"):
                # Open the order menu
                self.__order_menu()
            if selection == self.loc.get("menu_rate"):
                # Open the order menu
                self.__rate_menu()

    def __rate_menu(self):
        rate_kb = [[telegram.KeyboardButton(self.loc.get("menu_rate_5"))],
                   [telegram.KeyboardButton(self.loc.get("menu_rate_4"))],
                   [telegram.KeyboardButton(self.loc.get("menu_rate_3"))],
                   [telegram.KeyboardButton(self.loc.get("menu_rate_2"))],
                   [telegram.KeyboardButton(self.loc.get("menu_rate_1"))]]
        self.bot.send_message(self.chat.id, self.loc.get("conversation_rate"),
                              reply_markup=telegram.ReplyKeyboardMarkup(rate_kb, resize_keyboard=True))
        rate = self.__wait_for_specific_message([self.loc.get("menu_rate_5"),
                                                 self.loc.get("menu_rate_4"),
                                                 self.loc.get("menu_rate_3"),
                                                 self.loc.get("menu_rate_2"),
                                                 self.loc.get("menu_rate_1")],
                                                cancellable=False)
        self.bot.send_message(self.chat.id, self.loc.get("conversation_rate_notes"),
                              reply_markup=telegram.ReplyKeyboardMarkup(
                                  [[telegram.KeyboardButton(self.loc.get("menu_skip"))]],
                                  resize_keyboard=True
                              ))
        notes = self.__wait_for_regex(r"(.*)")
        if notes == self.loc.get("menu_skip"):
            notes = ""
        new_rate = self.loc.get("new_rate_text",
                                user=self.user.mention(),
                                rate=rate,
                                comment=notes)
        self.bot.send_message(self.cfg["Administration"]["rates_channel"], new_rate)

    def __order_menu(self):
        level = [None]
        cart: Dict[List[db.Product, int, db.Size]] = {}
        while True:
            categories = self.session.query(db.Category).filter_by(is_active=True, deleted=False,
                                                                   parent_id=level[-1]).all()
            products = self.session.query(db.Product).filter_by(deleted=False, category_id=level[-1]).all()
            # buttons = [[telegram.KeyboardButton(self.loc.get("menu_home"))],
            #            [telegram.KeyboardButton(self.loc.get("menu_cart"))]]
            buttons = []
            category_names = []
            product_names = []
            row = []
            for category in categories:
                category_names.append(str(category.name))
                row.append(telegram.KeyboardButton(str(category.name)))
                if len(row) == 2:
                    buttons.append(row)
                    row = []
            if len(products) != 0:
                for product in products:
                    product_names.append(product.name)
                    if len(row) == 1:
                        row.append(telegram.KeyboardButton(product.name))
                    else:
                        if len(buttons) - 2 == (len(categories) + len(products) - 1) / 2:
                            buttons.append([telegram.KeyboardButton(product.name)])
                        else:
                            row.append(telegram.KeyboardButton(product.name))
                    if len(row) == 2:
                        buttons.append(row)
                        row = []
            else:
                if len(row) != 0:
                    buttons.append(row)
            buttons.append([telegram.KeyboardButton(self.loc.get("menu_cart"))])
            buttons.append([telegram.KeyboardButton(self.loc.get("menu_home"))])
            if level[-1] is not None:
                buttons[-1].append(telegram.KeyboardButton(self.loc.get("menu_back")))
            message = self.bot.send_message(self.chat.id, self.loc.get("conversation_choose_item"),
                                            reply_markup=telegram.ReplyKeyboardMarkup(
                                                buttons, one_time_keyboard=False,
                                                resize_keyboard=True))
            choice = self.__wait_for_specific_message(category_names + product_names \
                                                      + [self.loc.get("menu_back"),
                                                         self.loc.get("menu_home"),
                                                         self.loc.get("menu_cart")], cancellable=True)
            if choice == self.loc.get("menu_home"):
                self.bot.delete_message(self.chat.id, message.message_id)
                break
            elif choice == self.loc.get("menu_back"):
                self.bot.delete_message(self.chat.id, message.message_id)
                level.pop(-1)
                pass
            elif choice == self.loc.get("menu_cart"):
                self.bot.delete_message(self.chat.id, message.message_id)
                cart = self.__check_cart(cart=cart)
                if len(cart) == 0:
                    break
            elif choice in category_names:
                self.bot.delete_message(self.chat.id, message.message_id)
                category = self.session.query(db.Category).filter_by(is_active=True, deleted=False,
                                                                     name=choice).one()
                level.append(category.id)
            elif choice in product_names:
                self.bot.delete_message(self.chat.id, message.message_id)
                product = self.session.query(db.Product).filter_by(deleted=False, name=choice).one()
                try:
                    p_size = cart[product.id][2]
                    p_qty = cart[product.id][1]
                    cart[product.id] = [product, p_qty, p_size]
                except:
                    cart[product.id] = [product, 0, None]
                cart = self.__product_pre_set_menu(product=product, cart=cart)
        return

    def __product_pre_set_menu(self, cart, product):
        message = product.send_as_message(w=self, chat_id=self.chat.id, session=self.session)
        if len(product.children) != 0:
            sizes_list = []
            row = []
            for size in product.children:
                if size.deleted is False:
                    row.append(telegram.InlineKeyboardButton(
                        str(size.name + " - " + str(size.price)), callback_data=str(size.id)))
            sizes_list.append(row)
            sizes_keyboard = telegram.InlineKeyboardMarkup(sizes_list)
            size_msg = self.bot.send_message(self.chat.id, self.loc.get("conversation_select_product_size"),
                                             reply_markup=sizes_keyboard)
            callback = self.__wait_for_inlinekeyboard_callback()
            size = self.session.query(db.Size).filter_by(deleted=False, id=int(callback.data)).one()
            size_id = size.id
            p = cart.get(product.id)
            p[2] = size
            self.bot.delete_message(self.chat.id, size_msg.message_id)
        else:
            size_id = None
        inline_buttons = []
        row = []
        for i in range(1, 13):
            row.append(telegram.InlineKeyboardButton(str(i), callback_data=str(i)))
            if len(row) == 4:
                inline_buttons.append(row)
                row = []
        if cart[product.id][1] != 0:
            inline_buttons.append([telegram.InlineKeyboardButton(self.loc.get("menu_remove_from_cart"),
                                                                 callback_data="cart_remove")])
        inline_keyboard = telegram.InlineKeyboardMarkup(inline_buttons)
        # Edit the sent message and add the inline keyboard
        if product.image is None:
            self.bot.edit_message_text(chat_id=self.chat.id,
                                       message_id=message['result']['message_id'],
                                       text=product.text(w=self, cart_qty=cart[product.id][1],
                                                         size_id=size_id,
                                                         session=self.session),
                                       reply_markup=inline_keyboard)
        else:
            self.bot.edit_message_caption(chat_id=self.chat.id,
                                          message_id=message['result']['message_id'],
                                          caption=product.text(w=self, cart_qty=cart[product.id][1],
                                                               size_id=size_id,
                                                               session=self.session),
                                          reply_markup=inline_keyboard)
        callback = self.__wait_for_inlinekeyboard_callback()
        if callback.data == "cart_remove":
            cart[product.id][1] = 0
            self.bot.delete_message(self.chat.id, message['result']['message_id'])
            self.bot.send_message(self.chat.id, self.loc.get("success_product_removed_from_cart",
                                                             product=cart.get(product.id)[0]))
        else:
            # Get the selected product, ensuring it exists
            p = cart.get(product.id)
            product = p[0]
            # Add 1 copy to the cart
            cart[product.id][1] += int(callback.data)
            self.bot.delete_message(self.chat.id, message['result']['message_id'])
            qty = replace_digits_to_emoji(text=str(cart[product.id][1]))
            name = product.name + (" " + p[2].name if p[2] is not None else "")
            self.bot.send_message(self.chat.id, self.loc.get("success_product_added_to_cart",
                                                             name=name,
                                                             qty=qty))
        return cart

    def __check_cart(self, cart):
        while True:
            if len(cart) == 0:
                self.bot.send_message(self.chat.id, self.loc.get("error_cart_empty"))
                return cart
            inline_buttons = [[telegram.InlineKeyboardButton(self.loc.get("menu_cancel"),
                                                             callback_data="cmd_cancel"),
                               telegram.InlineKeyboardButton(self.loc.get("menu_done"),
                                                             callback_data="cmd_done")]]
            cart_list = []
            total = self.Price(0)
            for product in cart:
                if cart[product][2] is not None:
                    amount = cart[product][1] * self.Price(cart[product][2].price)
                else:
                    amount = cart[product][1] * self.Price(cart[product][0].price)
                size_name = " " + cart[product][2].name if cart[product][2] is not None else ""
                cart_list.append(replace_digits_to_emoji(str(cart[product][1])) \
                                 + "x " + cart[product][0].name + size_name + " = " + str(amount))
                inline_buttons.append([telegram.InlineKeyboardButton(str("✖️ " + cart[product][0].name + " ✖️"),
                                                                     callback_data=str(cart[product][0].id))])
                if cart[product][2] is not None:
                    price = cart[product][2].price
                else:
                    price = cart[product][0].price
                total += cart[product][1] * price
            cart_str = "~ " + "\n~ ".join(cart_list)
            message = self.bot.send_message(self.chat.id, self.loc.get("conversation_check_cart",
                                                                       cart_str=cart_str,
                                                                       total=total),
                                            reply_markup=telegram.InlineKeyboardMarkup(inline_buttons))
            callback = self.__wait_for_inlinekeyboard_callback(cancellable=True)
            if isinstance(callback, CancelSignal):
                self.bot.delete_message(self.chat.id, message.message_id)
                return cart
            elif callback.data == "cmd_done":
                cart = self.__confirm_order(cart=cart, message_id=message.message_id, cart_str=cart_str, total=total)
                cart: Dict[List[db.Product, int]] = {}
                return cart
            else:
                self.bot.delete_message(self.chat.id, message.message_id)
                del cart[int(callback.data)]
                continue
        return

    def __confirm_order(self, cart, message_id, cart_str, total):
        while True:
            inline_markup_address = telegram.InlineKeyboardMarkup([[
                telegram.InlineKeyboardButton(self.loc.get("menu_cancel"), callback_data="cmd_cancel"),
                telegram.InlineKeyboardButton(self.loc.get("menu_pickup"), callback_data="cmd_pickup")]])
            location_markup = telegram.ReplyKeyboardMarkup([[
                telegram.KeyboardButton(self.loc.get("menu_location"), request_location=True)
            ]], resize_keyboard=True)
            # TODO: Выводить inline подсказки с предыдущими адресами
            self.bot.send_message(self.chat.id, self.loc.get("ask_for_address"),
                                  reply_markup=location_markup)
            self.bot.edit_message_text(chat_id=self.chat.id,
                                       message_id=message_id,
                                       text=self.loc.get("ask_for_address"),
                                       reply_markup=inline_markup_address)
            answer = self.__wait_for_inlinekeyboard_callback(accept_location=True, accept_text=True, cancellable=True)
            if not isinstance(answer, CallbackQuery):
                is_pickup = False
                if answer.location:
                    location = answer.location
                    address = self.loc.get("text_location")
                else:
                    location = None
                    address = answer.text
            elif answer.data == "cmd_pickup":
                is_pickup = True
                location = None
                address = self.loc.get("menu_pickup")
            phone_request = telegram.ReplyKeyboardMarkup([[
                telegram.KeyboardButton(self.loc.get("menu_share_phone"), request_contact=True)
            ]], resize_keyboard=True, one_time_keyboard=True)
            self.bot.send_message(self.chat.id, self.loc.get("ask_for_phone"), reply_markup=phone_request)
            phone = self.__wait_for_contact()
            skip_markup = telegram.InlineKeyboardMarkup([[
                telegram.InlineKeyboardButton(self.loc.get("menu_skip"), callback_data="cmd_cancel")
            ]])
            self.bot.send_message(self.chat.id, self.loc.get("ask_order_notes"), reply_markup=skip_markup)
            # TODO: Выбор формы оплаты
            notes = self.__wait_for_regex(r"(.*)", cancellable=True)
            if isinstance(notes, CancelSignal):
                notes = ""
            confirm = telegram.InlineKeyboardMarkup([[
                telegram.InlineKeyboardButton(self.loc.get("menu_confirm"), callback_data="cmd_confirm")
            ],
                [telegram.InlineKeyboardButton(self.loc.get("menu_cancel"), callback_data="cmd_cancel")]])
            final_text = self.loc.get("ask_final_confirmation",
                                      cart_str=cart_str,
                                      total_amount=total,
                                      address=address,
                                      comment=notes)
            self.bot.send_message(self.chat.id, final_text, reply_markup=confirm)
            callback = self.__wait_for_inlinekeyboard_callback(cancellable=True)
            if isinstance(callback, CancelSignal):
                return cart
            elif callback.data == "cmd_confirm":
                break
        if location is not None:
            latitude = location.latitude
            longitude = location.longitude
        else:
            latitude = None
            longitude = None
        new_address = db.Address(
            text=address,
            latitude=latitude,
            longitude=longitude,
            user_id=self.chat.id,
            deleted=False
        )
        self.session.add(new_address)
        self.session.flush()
        # Create a new Order
        order = db.Order(user=self.user,
                         is_pickup=is_pickup,
                         phone=phone,
                         address_id=new_address.id,
                         creation_date=datetime.datetime.now(),
                         notes=notes if not isinstance(notes, CancelSignal) else "")
        # Add the record to the session and get an ID
        self.session.add(order)
        self.session.flush()
        # For each product added to the cart, create a new OrderItem
        for product in cart:
            if cart[product][2] is not None:
                size_id = cart[product][2].id
            else:
                size_id = None
            # Create {quantity} new OrderItems
            for i in range(0, cart[product][1]):
                order_item = db.OrderItem(product=cart[product][0],
                                          order_id=order.order_id,
                                          size_id=size_id)
                self.session.add(order_item)
        self.bot.send_message(self.chat.id, self.loc.get("success_order_created",
                                                         order=order.order_id))
        # TODO: ссылка на оплату, если это не наличка
        new_order_text = self.loc.get("new_order_text",
                                      cart=cart_str,
                                      amount=total,
                                      address=address,
                                      name=self.user.mention(),
                                      phone=phone,
                                      comment=notes)
        self.bot.send_message(self.cfg["Administration"]["orders_channel"], new_order_text)
        if location is not None:
            self.bot.send_location(chat_id=self.cfg["Administration"]["orders_channel"],
                                   latitude=location.latitude,
                                   longitude=location.longitude)
        self.session.commit()

    def __get_cart_value(self, cart):
        # Calculate total items value in cart
        value = self.Price(0)
        for product in cart:
            value += cart[product][0].price * cart[product][1]
        return value

    def __get_cart_summary(self, cart):
        # Create the cart summary
        product_list = ""
        for product_id in cart:
            if cart[product_id][1] > 0:
                product_list += cart[product_id][0].text(w=self,
                                                         style="short",
                                                         cart_qty=cart[product_id][1]) + "\n"
        return product_list

    def __order_transaction(self, order, value):
        # Create a new transaction and add it to the session
        transaction = db.Transaction(user=self.user,
                                     value=value,
                                     order_id=order.order_id)
        self.session.add(transaction)
        # Commit all the changes
        self.session.commit()
        # Notify admins about new transation
        self.__order_notify_admins(order=order)

    def __order_notify_admins(self, order):
        # Notify the user of the order result
        self.bot.send_message(self.chat.id, self.loc.get("success_order_created", order=order.text(w=self,
                                                                                                   session=self.session,
                                                                                                   user=True)))
        # Notify the admins (in Live Orders mode) of the new order
        admins = self.session.query(db.Admin).filter_by(live_mode=True).all()
        # Create the order keyboard
        order_keyboard = telegram.InlineKeyboardMarkup(
            [
                [telegram.InlineKeyboardButton(self.loc.get("menu_complete"), callback_data="order_complete")],
                [telegram.InlineKeyboardButton(self.loc.get("menu_refund"), callback_data="order_refund")]
            ])
        # Notify them of the new placed order
        for admin in admins:
            self.bot.send_message(admin.user_id,
                                  self.loc.get('notification_order_placed',
                                               order=order.text(w=self, session=self.session)),
                                  reply_markup=order_keyboard)

    def __order_status(self):
        """Display the status of the sent orders."""
        log.debug("Displaying __order_status")
        # Find the latest orders
        orders = self.session.query(db.Order) \
            .filter(db.Order.user == self.user) \
            .order_by(db.Order.creation_date.desc()) \
            .limit(20) \
            .all()
        # Ensure there is at least one order to display
        if len(orders) == 0:
            self.bot.send_message(self.chat.id, self.loc.get("error_no_orders"))
        # Display the order status to the user
        for order in orders:
            self.bot.send_message(self.chat.id, order.text(w=self, session=self.session, user=True))
        # TODO: maybe add a page displayer instead of showing the latest 5 orders

    def __bot_info(self):
        """Send information about the bot."""
        log.debug("Displaying __bot_info")
        self.bot.send_message(self.chat.id, self.loc.get("bot_info"))

    def __admin_menu(self):
        """Function called from the run method when the user is an administrator.
        Administrative bot actions should be placed here."""
        log.debug("Displaying __admin_menu")
        # Loop used to return to the menu after executing a command
        while True:
            # Create a keyboard with the admin main menu based on the admin permissions specified in the db
            keyboard = []
            if self.admin.edit_products:
                keyboard.append([self.loc.get("menu_products"),
                                 self.loc.get("menu_categories")])
            if self.admin.is_owner:
                keyboard.append([self.loc.get("menu_edit_admins")])
            keyboard.append([self.loc.get("menu_user_mode")])
            # Send the previously created keyboard to the user (ensuring it can be clicked only 1 time)
            self.bot.send_message(self.chat.id, self.loc.get("conversation_open_admin_menu"),
                                  reply_markup=telegram.ReplyKeyboardMarkup(keyboard, one_time_keyboard=True,
                                                                            resize_keyboard=True))
            # Wait for a reply from the user
            # TODO: Настройка форм оплаты: добавление, настройка, включение и выключение, удаление
            selection = self.__wait_for_specific_message([self.loc.get("menu_products"),
                                                          self.loc.get("menu_categories"),
                                                          # self.loc.get("menu_orders"),
                                                          self.loc.get("menu_user_mode"),
                                                          self.loc.get("menu_edit_admins")])
            # If the user has selected the Products option...
            if selection == self.loc.get("menu_products"):
                # Open the products menu
                self.__products_menu()
            # If the user has selected the Categories option...
            if selection == self.loc.get("menu_categories"):
                # Open the categories menu
                self.__categories_menu()
            # If the user has selected the User mode option...
            elif selection == self.loc.get("menu_user_mode"):
                # Tell the user how to go back to admin menu
                self.bot.send_message(self.chat.id, self.loc.get("conversation_switch_to_user_mode"))
                # Start the bot in user mode
                self.__user_menu()
            # If the user has selected the Add Admin option...
            elif selection == self.loc.get("menu_edit_admins"):
                # Open the edit admin menu
                self.__add_admin()

    def __categories_menu(self):
        """Display the admin menu to select a category to edit."""
        log.debug("Displaying __categories_menu")
        # Get the categories list from the db
        categories = self.session.query(db.Category).filter_by(deleted=False).all()
        # Create a list of category names
        category_names = [category.name for category in categories]
        # Insert at the start of the list the add category option, the remove category option and the Cancel option
        category_names.insert(0, self.loc.get("menu_cancel"))
        category_names.insert(1, self.loc.get("menu_add_category"))
        category_names.insert(2, self.loc.get("menu_delete_category"))
        # Create a keyboard using the category names
        keyboard = [[telegram.KeyboardButton(category_name)] for category_name in category_names]
        # Send the previously created keyboard to the user (ensuring it can be clicked only 1 time)
        self.bot.send_message(self.chat.id, self.loc.get("conversation_admin_select_category"),
                              reply_markup=telegram.ReplyKeyboardMarkup(keyboard, one_time_keyboard=True,
                                                                        resize_keyboard=True))
        # Wait for a reply from the user
        selection = self.__wait_for_specific_message(category_names, cancellable=True)
        # If the user has selected the Cancel option...
        if isinstance(selection, CancelSignal):
            # Exit the menu
            return
        # If the user has selected the Add Category option...
        elif selection == self.loc.get("menu_add_category"):
            # Open the add category menu
            self.__edit_category_menu()
        # If the user has selected the Remove Category option...
        elif selection == self.loc.get("menu_delete_category"):
            # Open the delete category menu
            self.__delete_category_menu()
        # If the user has selected a category
        else:
            # Find the selected category
            category = self.session.query(db.Category).filter_by(name=selection, deleted=False).one()
            # Open the edit menu for that specific category
            self.__edit_category_menu(category=category)

    def __edit_category_menu(self, category: Optional[db.Category] = None):
        """Add a category to the database or edit an existing one."""
        log.debug("Displaying __edit_category_menu")
        # Create an inline keyboard with a single skip button
        cancel = telegram.InlineKeyboardMarkup([[telegram.InlineKeyboardButton(self.loc.get("menu_skip"),
                                                                               callback_data="cmd_cancel")]])
        # Ask for the category name until a valid category name is specified
        while True:
            # Ask the question to the user
            self.bot.send_message(self.chat.id, self.loc.get("ask_category_name"))
            # Display the current name if you're editing an existing category
            if category:
                self.bot.send_message(self.chat.id, self.loc.get("edit_current_value", value=escape(category.name)),
                                      reply_markup=cancel)
            # Wait for an answer
            name = self.__wait_for_regex(r"(.*)", cancellable=bool(category))
            # Ensure a product with that name doesn't already exist
            if (category and isinstance(name, CancelSignal)) or \
                    self.session.query(db.Category).filter_by(name=name, deleted=False).one_or_none() in [None,
                                                                                                          category]:
                # Exit the loop
                break
            self.bot.send_message(self.chat.id, self.loc.get("error_duplicate_name"))
        if category:
            parents = self.session.query(db.Category).filter_by(deleted=False, is_active=True) \
                .filter(db.Category.id != category.id).all()
        else:
            parents = self.session.query(db.Category).filter_by(deleted=False, is_active=True).all()
            parent_id = None
        if len(parents) != 0:
            parent_id = self.__assign_category(category=category, product=None)
        if not category:
            name = name if not isinstance(name, CancelSignal) else category.name
            new_category = db.Category(
                name=name,
                is_active=True,
                deleted=False,
                parent_id=parent_id
            )
            self.session.add(new_category)
            self.bot.send_message(self.chat.id, self.loc.get("success_added_category", name=name))
        else:
            name = name if not isinstance(name, CancelSignal) else category.name
            category.name = name
            category.parent_id = parent_id
            self.bot.send_message(self.chat.id, self.loc.get("success_edited_category", name=name))
        self.session.commit()

    def __assign_category(self, category, product):
        if category:
            parents = self.session.query(db.Category).filter_by(deleted=False, is_active=True) \
                .filter(db.Category.id != category.id).all()
            current = category.parent.name if category.parent is not None else self.loc.get("text_not_defined")
        else:
            parents = self.session.query(db.Category).filter_by(deleted=False, is_active=True).all()
            parent_id = None
            current = self.loc.get("text_not_defined")
        skip_markup = telegram.InlineKeyboardMarkup([[telegram.InlineKeyboardButton(
            self.loc.get("menu_skip"), callback_data="cmd_cancel"
        )]])
        parent_buttons = [
            [telegram.KeyboardButton(self.loc.get("menu_no_category"))]
        ]
        row = []
        for parent in parents:
            row.append(telegram.KeyboardButton(parent.name))
            if len(row) == 2:
                parent_buttons.append(row)
                row = []
            if len(parent_buttons) - 1 == (len(parents) - 1) / 2:
                parent_buttons.append([telegram.KeyboardButton(parent.name)])
        self.bot.send_message(self.chat.id, self.loc.get("conversation_admin_select_parent_category",
                                                         current=current),
                              reply_markup=telegram.ReplyKeyboardMarkup(parent_buttons, resize_keyboard=True,
                                                                        one_time_keyboard=True))
        skip_msg = self.bot.send_message(self.chat.id, self.loc.get("conversation_skip_parent_assignment"),
                                         reply_markup=skip_markup)
        choice = self.__wait_for_specific_message(
            [parent.name for parent in parents] + [self.loc.get("menu_no_category")],
            cancellable=True)
        if isinstance(choice, CancelSignal):
            if category:
                parent_id = category.parent.id if category.parent is not None else None
            else:
                parent_id = None
        elif choice == self.loc.get("menu_no_category"):
            parent_id = None
        else:
            parent_id = self.session.query(db.Category).filter_by(name=choice, deleted=False).one().id
            # parent_id = parent.id
        return parent_id

    def __delete_category_menu(self):
        log.debug("Displaying __delete_category_menu")
        # Get the categories list from the db
        categories = self.session.query(db.Category).filter_by(deleted=False).all()
        # Create a list of category names
        category_names = [category.name for category in categories]
        # Insert at the start of the list the Cancel button
        category_names.insert(0, self.loc.get("menu_cancel"))
        # Create a keyboard using the category names
        keyboard = [[telegram.KeyboardButton(category_name)] for category_name in category_names]
        # Send the previously created keyboard to the user (ensuring it can be clicked only 1 time)
        self.bot.send_message(self.chat.id, self.loc.get("conversation_admin_select_category_to_delete"),
                              reply_markup=telegram.ReplyKeyboardMarkup(keyboard, one_time_keyboard=True))
        # Wait for a reply from the user
        selection = self.__wait_for_specific_message(category_names, cancellable=True)
        if isinstance(selection, CancelSignal):
            # Exit the menu
            return
        else:
            # Find the selected category
            category = self.session.query(db.Category).filter_by(name=selection, deleted=False).one()
            # "Delete" the category by setting the deleted flag to true
            category.deleted = True
            self.session.commit()
            # Notify the user
            self.bot.send_message(self.chat.id, self.loc.get("success_category_deleted"))

    def __products_menu(self):
        """Display the admin menu to select a product to edit."""
        log.debug("Displaying __products_menu")
        # Get the products list from the db
        products = self.session.query(db.Product).filter_by(deleted=False).all()
        # Create a list of product names
        product_names = [product.name for product in products]
        # Insert at the start of the list the add product option, the remove product option and the Cancel option
        product_names.insert(0, self.loc.get("menu_cancel"))
        product_names.insert(1, self.loc.get("menu_add_product"))
        product_names.insert(2, self.loc.get("menu_delete_product"))
        # Create a keyboard using the product names
        keyboard = [[telegram.KeyboardButton(product_name)] for product_name in product_names]
        # Send the previously created keyboard to the user (ensuring it can be clicked only 1 time)
        self.bot.send_message(self.chat.id, self.loc.get("conversation_admin_select_product"),
                              reply_markup=telegram.ReplyKeyboardMarkup(keyboard, one_time_keyboard=True,
                                                                        resize_keyboard=True))
        # Wait for a reply from the user
        selection = self.__wait_for_specific_message(product_names, cancellable=True)
        # If the user has selected the Cancel option...
        if isinstance(selection, CancelSignal):
            # Exit the menu
            return
        # If the user has selected the Add Product option...
        elif selection == self.loc.get("menu_add_product"):
            # Open the add product menu
            self.__edit_product_menu()
        # If the user has selected the Remove Product option...
        elif selection == self.loc.get("menu_delete_product"):
            # Open the delete product menu
            self.__delete_product_menu()
        # If the user has selected a product
        else:
            # Find the selected product
            product = self.session.query(db.Product).filter_by(name=selection, deleted=False).one()
            # Open the edit menu for that specific product
            self.__edit_product_menu(product=product)

    def __edit_product_menu(self, product: Optional[db.Product] = None):
        """Add a product to the database or edit an existing one."""
        log.debug("Displaying __edit_product_menu")
        # Create an inline keyboard with a single skip button
        cancel = telegram.InlineKeyboardMarkup([[telegram.InlineKeyboardButton(self.loc.get("menu_skip"),
                                                                               callback_data="cmd_cancel")]])
        category_id = self.__assign_category(category=None, product=product)
        # Ask for the product name until a valid product name is specified
        while True:
            # Ask the question to the user
            self.bot.send_message(self.chat.id, self.loc.get("ask_product_name"))
            # Display the current name if you're editing an existing product
            if product:
                self.bot.send_message(self.chat.id, self.loc.get("edit_current_value", value=escape(product.name)),
                                      reply_markup=cancel)
            # Wait for an answer
            name = self.__wait_for_regex(r"(.*)", cancellable=bool(product))
            # Ensure a product with that name doesn't already exist
            if (product and isinstance(name, CancelSignal)) or \
                    self.session.query(db.Product).filter_by(name=name, deleted=False).one_or_none() in [None, product]:
                # Exit the loop
                break
            self.bot.send_message(self.chat.id, self.loc.get("error_duplicate_name"))
        # Ask for the product description
        self.bot.send_message(self.chat.id, self.loc.get("ask_product_description"))
        # Display the current description if you're editing an existing product
        if product:
            self.bot.send_message(self.chat.id,
                                  self.loc.get("edit_current_value", value=escape(product.description)),
                                  reply_markup=cancel)
        # Wait for an answer
        description = self.__wait_for_regex(r"(.*)", cancellable=bool(product))
        if product:
            children = self.session.query(db.Size).filter_by(product_id=product.id, deleted=False).all()
            if len(children) != 0:
                current_sizes = self.loc.get("current_sizes",
                                             sizes_str="\n" \
                                             .join([child.name + " - " \
                                                    + str(child.price) \
                                                    + " " + self.cfg["Payments"]["currency_symbol"]
                                                    for child in children]))
            else:
                current_sizes = ""
        else:
            current_sizes = ""
            # Ask for product sizes
        self.bot.send_message(self.chat.id, self.loc.get("ask_product_sizes"))
        if current_sizes != "":
            self.bot.send_message(self.chat.id, current_sizes, reply_markup=cancel)
        # Accepts size list in format:
        # 12 [cm, см, сантиметров] - 123456
        sizes = self.__wait_for_regex(r"(((([\d ,.]{0,6}.{0,15}( - ){0,1}\d{4,9}\s{0,1}){1,5}|([XxХх]){1})))",
                                      cancellable=bool(product))
        if isinstance(sizes, CancelSignal):
            db_sizes = self.session.query(db.Size).filter_by(product_id=product.id, deleted=False).all()
            sizes = "\n".join([db_size.name + " - " + str(db_size.price) \
                               for db_size in db_sizes])
            price = None
            sizes = sizes.splitlines()
        elif (sizes.lower() == "x") or (sizes.lower() == "х"):
            sizes = None
        else:
            price = None
            sizes = sizes.splitlines()
        # If no size given, ask for the product price
        if sizes is None:
            # Delete previous product sizes if exists
            if product:
                prev_sizes = self.session.query(db.Size).filter_by(product_id=product.id).all()
                for prev_size in prev_sizes:
                    prev_size.deleted = True
            # Ask for the product price
            self.bot.send_message(self.chat.id,
                                  self.loc.get("ask_product_price"))
            # Display the current name if you're editing an existing product
            if product:
                self.bot.send_message(self.chat.id,
                                      self.loc.get("edit_current_value",
                                                   value=(str(self.Price(product.price))
                                                          if product.price is not None
                                                          else self.loc.get("not_in_price_list"))),
                                      reply_markup=cancel)
            # Wait for an answer
            price = self.__wait_for_regex(r"([0-9]+(?:[.,][0-9]{1,2})?|[XxХх])",
                                          cancellable=True)
            # If the price is skipped
            if isinstance(price, CancelSignal):
                pass
            elif price.lower() == "x":
                price = None
            else:
                price = self.Price(price)
        # Ask for the product image
        self.bot.send_message(self.chat.id, self.loc.get("ask_product_image"), reply_markup=cancel)
        # Wait for an answer
        photo_list = self.__wait_for_photo(cancellable=True)
        # If a new product is being added...
        if not product:
            # Create the db record for the product
            # noinspection PyTypeChecker
            product = db.Product(name=name,
                                 description=description,
                                 price=int(price) if price is not None else None,
                                 category_id=category_id,
                                 deleted=False)
            # Add the record to the database
            self.session.add(product)
            # Flush session to get product id
            self.session.flush()
            if sizes is not None:
                # Add new product sizes in database
                for size in sizes:
                    size_name = size[:size.find(" - ")]
                    size_price = size[size.find(" - ") + 3:]
                    db_size = db.Size(
                        product_id=product.id,
                        name=size_name,
                        price=size_price,
                        deleted=False
                    )
                    self.session.add(db_size)
        # If a product is being edited...
        else:
            # Edit the record with the new values
            product.category_id = category_id if not isinstance(category_id, CancelSignal) else product.category_id
            product.name = name if not isinstance(name, CancelSignal) else product.name
            product.description = description if not isinstance(description, CancelSignal) else product.description
            if price is not None:
                product.price = int(price) if not isinstance(price, CancelSignal) else product.price
            else:
                product.price = price
            if sizes is not None:
                # Delete previous sizes of product
                prev_sizes = self.session.query(db.Size).filter_by(product_id=product.id, deleted=False).all()
                for prev_size in prev_sizes:
                    prev_size.deleted = True
                # Add new product sizes in database
                for size in sizes:
                    size_name = size[:size.find(" - ")]
                    size_price = size[size.find(" - ") + 3:]
                    db_size = db.Size(
                        product_id=product.id,
                        name=size_name,
                        price=int(size_price),
                        deleted=False
                    )
                    self.session.add(db_size)
        # If a photo has been sent...
        if isinstance(photo_list, list):
            # Find the largest photo id
            largest_photo = photo_list[0]
            for photo in photo_list[1:]:
                if photo.width > largest_photo.width:
                    largest_photo = photo
            # Get the file object associated with the photo
            photo_file = self.bot.get_file(largest_photo.file_id)
            # Notify the user that the bot is downloading the image and might be inactive for a while
            self.bot.send_message(self.chat.id, self.loc.get("downloading_image"))
            self.bot.send_chat_action(self.chat.id, action="upload_photo")
            # Set the image for that product
            product.set_image(photo_file)
        # Commit the session changes
        self.session.commit()
        # Notify the user
        self.bot.send_message(self.chat.id, self.loc.get("success_product_edited"))

    def __delete_product_menu(self):
        log.debug("Displaying __delete_product_menu")
        # Get the products list from the db
        products = self.session.query(db.Product).filter_by(deleted=False).all()
        # Create a list of product names
        product_names = [product.name for product in products]
        # Insert at the start of the list the Cancel button
        product_names.insert(0, self.loc.get("menu_cancel"))
        # Create a keyboard using the product names
        keyboard = [[telegram.KeyboardButton(product_name)] for product_name in product_names]
        # Send the previously created keyboard to the user (ensuring it can be clicked only 1 time)
        self.bot.send_message(self.chat.id, self.loc.get("conversation_admin_select_product_to_delete"),
                              reply_markup=telegram.ReplyKeyboardMarkup(keyboard, one_time_keyboard=True))
        # Wait for a reply from the user
        selection = self.__wait_for_specific_message(product_names, cancellable=True)
        if isinstance(selection, CancelSignal):
            # Exit the menu
            return
        else:
            # Find the selected product
            product = self.session.query(db.Product).filter_by(name=selection, deleted=False).one()
            # "Delete" the product by setting the deleted flag to true
            product.deleted = True
            self.session.commit()
            # Notify the user
            self.bot.send_message(self.chat.id, self.loc.get("success_product_deleted"))

    def __help_menu(self):
        """Help menu. Allows the user to ask for assistance, get a guide or see some info about the bot."""
        log.debug("Displaying __help_menu")
        # Create a keyboard with the user help menu
        keyboard = [[telegram.KeyboardButton(self.loc.get("menu_guide"))],
                    [telegram.KeyboardButton(self.loc.get("menu_contact_shopkeeper"))],
                    [telegram.KeyboardButton(self.loc.get("menu_cancel"))]]
        # Send the previously created keyboard to the user (ensuring it can be clicked only 1 time)
        self.bot.send_message(self.chat.id,
                              self.loc.get("conversation_open_help_menu"),
                              reply_markup=telegram.ReplyKeyboardMarkup(keyboard, one_time_keyboard=True))
        # Wait for a reply from the user
        selection = self.__wait_for_specific_message([
            self.loc.get("menu_guide"),
            self.loc.get("menu_contact_shopkeeper")
        ], cancellable=True)
        # If the user has selected the Guide option...
        if selection == self.loc.get("menu_guide"):
            # Send them the bot guide
            self.bot.send_message(self.chat.id, self.loc.get("help_msg"))
        # If the user has selected the Order Status option...
        elif selection == self.loc.get("menu_contact_shopkeeper"):
            # Find the list of available shopkeepers
            shopkeepers = self.session.query(db.Admin).filter_by(display_on_help=True).join(db.User).all()
            # Create the string
            shopkeepers_string = "\n".join([admin.user.mention() for admin in shopkeepers])
            # Send the message to the user
            self.bot.send_message(self.chat.id, self.loc.get("contact_shopkeeper", shopkeepers=shopkeepers_string))
        # If the user has selected the Cancel option the function will return immediately

    def __add_admin(self):
        """Add an administrator to the bot."""
        log.debug("Displaying __add_admin")
        # Let the admin select an administrator to promote
        user = self.__user_select()
        # Allow the cancellation of the operation
        if isinstance(user, CancelSignal):
            return
        # Check if the user is already an administrator
        admin = self.session.query(db.Admin).filter_by(user_id=user.user_id).one_or_none()
        if admin is None:
            # Create the keyboard to be sent
            keyboard = telegram.ReplyKeyboardMarkup([[self.loc.get("emoji_yes"), self.loc.get("emoji_no")]],
                                                    one_time_keyboard=True, resize_keyboard=True)
            # Ask for confirmation
            self.bot.send_message(self.chat.id, self.loc.get("conversation_confirm_admin_promotion"),
                                  reply_markup=keyboard)
            # Wait for an answer
            selection = self.__wait_for_specific_message([self.loc.get("emoji_yes"), self.loc.get("emoji_no")])
            # Proceed only if the answer is yes
            if selection == self.loc.get("emoji_no"):
                return
            # Create a new admin
            admin = db.Admin(user=user,
                             edit_products=False,
                             is_owner=False,
                             display_on_help=False)
            self.session.add(admin)
        # Send the empty admin message and record the id
        message = self.bot.send_message(self.chat.id, self.loc.get("admin_properties", name=str(admin.user)))
        # Start accepting edits
        while True:
            # Create the inline keyboard with the admin status
            inline_keyboard = telegram.InlineKeyboardMarkup([
                [telegram.InlineKeyboardButton(
                    f"{self.loc.boolmoji(admin.edit_products)} {self.loc.get('prop_edit_products')}",
                    callback_data="toggle_edit_products"
                )],
                [telegram.InlineKeyboardButton(
                    f"{self.loc.boolmoji(admin.display_on_help)} {self.loc.get('prop_display_on_help')}",
                    callback_data="toggle_display_on_help"
                )],
                [telegram.InlineKeyboardButton(
                    self.loc.get('menu_done'),
                    callback_data="cmd_done"
                )]
            ])
            # Update the inline keyboard
            self.bot.edit_message_reply_markup(message_id=message.message_id,
                                               chat_id=self.chat.id,
                                               reply_markup=inline_keyboard)
            # Wait for an user answer
            callback = self.__wait_for_inlinekeyboard_callback()
            # Toggle the correct property
            if callback.data == "toggle_edit_products":
                admin.edit_products = not admin.edit_products
            elif callback.data == "toggle_display_on_help":
                admin.display_on_help = not admin.display_on_help
            elif callback.data == "cmd_done":
                break
        self.session.commit()

    def __language_menu(self):
        """Select a language."""
        log.debug("Displaying __language_menu")
        keyboard = []
        options: Dict[str, str] = {}
        # https://en.wikipedia.org/wiki/List_of_language_names
        if "en" in self.cfg["Language"]["enabled_languages"]:
            lang = "🇬🇧 English"
            keyboard.append([telegram.KeyboardButton(lang)])
            options[lang] = "en"
        if "ru" in self.cfg["Language"]["enabled_languages"]:
            lang = "🇷🇺 Русский"
            keyboard.append([telegram.KeyboardButton(lang)])
            options[lang] = "ru"
        # Send the previously created keyboard to the user (ensuring it can be clicked only 1 time)
        self.bot.send_message(self.chat.id,
                              self.loc.get("conversation_language_select"),
                              reply_markup=telegram.ReplyKeyboardMarkup(keyboard, one_time_keyboard=True))
        # Wait for an answer
        response = self.__wait_for_specific_message(list(options.keys()))
        # Set the language to the corresponding value
        self.user.language = options[response]
        # Commit the edit to the database
        self.session.commit()
        # Recreate the localization object
        self.__create_localization()

    def __create_localization(self):
        # Check if the user's language is enabled; if it isn't, change it to the default
        if self.user.language not in self.cfg["Language"]["enabled_languages"]:
            log.debug(f"User's language '{self.user.language}' is not enabled, changing it to the default")
            self.user.language = self.cfg["Language"]["default_language"]
            self.session.commit()
        # Create a new Localization object
        self.loc = localization.Localization(
            language=self.user.language,
            fallback=self.cfg["Language"]["fallback_language"],
            replacements={
                "user_string": str(self.user),
                "user_mention": self.user.mention(),
                "user_full_name": self.user.full_name,
                "user_first_name": self.user.first_name,
                "today": datetime.datetime.now().strftime("%a %d %b %Y"),
            }
        )

    def __graceful_stop(self, stop_trigger: StopSignal):
        """Handle the graceful stop of the thread."""
        log.debug("Gracefully stopping the conversation")
        # If the session has expired...
        if stop_trigger.reason == "timeout":
            # Notify the user that the session has expired and remove the keyboard
            self.bot.send_message(self.chat.id, self.loc.get('conversation_expired'),
                                  reply_markup=telegram.ReplyKeyboardRemove())
        # If a restart has been requested...
        # Do nothing.
        # Close the database session
        self.session.close()
        # End the process
        sys.exit(0)
