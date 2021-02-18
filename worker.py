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
                                      receive_orders=True,
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

    def __wait_for_inlinekeyboard_callback(self, accept_location: bool = False, cancellable: bool = False) \
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
                if update.message.location is not None:
                    return update.message
                else:
                    continue
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
        keyboard = telegram.ReplyKeyboardMarkup(keyboard_buttons, one_time_keyboard=True)
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
            keyboard = [[telegram.KeyboardButton(self.loc.get("menu_order"))],
                        [telegram.KeyboardButton(self.loc.get("menu_order_status"))],
                        [telegram.KeyboardButton(self.loc.get("menu_language"))],
                        [telegram.KeyboardButton(self.loc.get("menu_help")),
                         telegram.KeyboardButton(self.loc.get("menu_bot_info"))]]
            # Send the previously created keyboard to the user (ensuring it can be clicked only 1 time)
            self.bot.send_message(self.chat.id,
                                  self.loc.get("conversation_open_user_menu"),
                                  reply_markup=telegram.ReplyKeyboardMarkup(keyboard, one_time_keyboard=True))
            # Wait for a reply from the user
            selection = self.__wait_for_specific_message([
                self.loc.get("menu_order"),
                self.loc.get("menu_order_status"),
                self.loc.get("menu_language"),
                self.loc.get("menu_help"),
                self.loc.get("menu_bot_info"),
            ])
            # After the user reply, update the user data
            self.update_user()
            # If the user has selected the Order option...
            if selection == self.loc.get("menu_order"):
                # Open the order menu
                self.__order_menu()
            # If the user has selected the Order Status option...
            elif selection == self.loc.get("menu_order_status"):
                # Display the order(s) status
                self.__order_status()
            # If the user has selected the Language option...
            elif selection == self.loc.get("menu_language"):
                # Display the language menu
                self.__language_menu()
            # If the user has selected the Bot Info option...
            elif selection == self.loc.get("menu_bot_info"):
                # Display information about the bot
                self.__bot_info()
            # If the user has selected the Help option...
            elif selection == self.loc.get("menu_help"):
                # Go to the Help menu
                self.__help_menu()

    def __order_menu(self):
        level = [None]
        cart: Dict[List[db.Product, int]] = {}
        while True:
            categories = self.session.query(db.Category).filter_by(active=True, deleted=False, parent=level[-1]).all()
            products = self.session.query(db.Product).filter_by(deleted=False, category_id=level[-1]).all()
            buttons = [[telegram.KeyboardButton(self.loc.get("menu_home"))],
                       [telegram.KeyboardButton(self.loc.get("menu_cart"))]]
            if level[-1] is not None:
                buttons[0].append(telegram.KeyboardButton(self.loc.get("menu_back")))
            category_names = []
            product_names = []
            row = []
            for category in categories:
                category_names.append("🗂 " + category.name)
                row.append(telegram.KeyboardButton("🗂 " + category.name))
                if len(row) == 2:
                    buttons.append(row)
                    row = []
            for product in products:
                product_names.append(product.name)
                if len(row) == 1:
                    row.append(telegram.KeyboardButton(product.name))
                else:
                    if len(buttons) - 1 == (len(categories) + len(products) - 1) / 2:
                        buttons.append([telegram.KeyboardButton(product.name)])
                    else:
                        row.append(telegram.KeyboardButton(product.name))
                if len(row) == 2:
                    buttons.append(row)
                    row = []
            message = self.bot.send_message(self.chat.id, self.loc.get("conversation_choose_item"),
                                            reply_markup=telegram.ReplyKeyboardMarkup(
                                                buttons,
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
                    # TODO: поблагодарить за заказ
                    break
            elif choice in category_names:
                self.bot.delete_message(self.chat.id, message.message_id)
                category = self.session.query(db.Category).filter_by(active=True, deleted=False, name=choice[2:]).one()
                level.append(category.id)
            elif choice in product_names:
                self.bot.delete_message(self.chat.id, message.message_id)
                product = self.session.query(db.Product).filter_by(deleted=False, name=choice).one()
                try:
                    p_qty = cart[product.id][1]
                    cart[product.id] = [product, p_qty]
                except:
                    cart[product.id] = [product, 0]
                cart = self.__product_pre_set_menu(product=product, cart=cart)
        return

    def __product_pre_set_menu(self, cart, product: tuple = None):
        message = product.send_as_message(w=self, chat_id=self.chat.id)
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
                                       text=product.text(w=self, cart_qty=cart[product.id][1]),
                                       reply_markup=inline_keyboard)
        else:
            self.bot.edit_message_caption(chat_id=self.chat.id,
                                          message_id=message['result']['message_id'],
                                          caption=product.text(w=self, cart_qty=cart[product.id][1]),
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
            self.bot.send_message(self.chat.id, self.loc.get("success_product_added_to_cart",
                                                             name=product.name,
                                                             qty=qty))
        return cart

    def __check_cart(self, cart):
        while True:
            if len(cart) == 0:
                # TODO: сообщение с текстом "корзина пуста"
                return cart
            inline_buttons = [[telegram.InlineKeyboardButton(self.loc.get("menu_cancel"),
                                                             callback_data="cmd_cancel"),
                               telegram.InlineKeyboardButton(self.loc.get("menu_done"),
                                                             callback_data="cmd_done")]]
            cart_str = ""
            total = self.Price(0)
            for product in cart:
                cart_str = "~ " + "\n~".join([replace_digits_to_emoji(str(cart[product][1])) \
                                              + "x " + cart[product][0].name])
                inline_buttons.append([telegram.InlineKeyboardButton(str("✖️ " + cart[product][0].name + " ✖️"),
                                                                     callback_data=str(cart[product][0].id))])
                total += cart[product][1] * cart[product][0].price
            message = self.bot.send_message(self.chat.id, self.loc.get("conversation_check_cart",
                                                                       cart_str=cart_str,
                                                                       total=total),
                                            reply_markup=telegram.InlineKeyboardMarkup(inline_buttons))
            callback = self.__wait_for_inlinekeyboard_callback(cancellable=True)
            if callback.data == "cmd_cancel":
                self.bot.delete_message(self.chat.id, message.message_id)
                return cart
            elif callback.data == "cmd_done":
                self.__confirm_order(cart=cart, message_id=message.message_id)
                cart: Dict[List[db.Product, int]] = {}
                return cart
            else:
                self.bot.delete_message(self.chat.id, message.message_id)
                del cart[int(callback.data)]
                continue
        return

    def __confirm_order(self, cart, message_id):
        while True:
            inline_markup_address = telegram.InlineKeyboardMarkup([[
                telegram.InlineKeyboardButton(self.loc.get("menu_cancel"), callback_data="cmd_cancel"),
                telegram.InlineKeyboardButton(self.loc.get("menu_pickup"), callback_data="cmd_pickup")]])
            location_markup = telegram.ReplyKeyboardMarkup([[
                telegram.KeyboardButton(self.loc.get("menu_location"), request_location=True)
            ]], resize_keyboard=True)
            self.bot.send_message(self.chat.id, self.loc.get("ask_for_address"),
                                  reply_markup=location_markup)
            self.bot.edit_message_text(chat_id=self.chat.id,
                                       message_id=message_id,
                                       text=self.loc.get("ask_for_address"),
                                       reply_markup=inline_markup_address)
            answer = self.__wait_for_inlinekeyboard_callback(accept_location=True, cancellable=True)
            if answer.location is not None:
                location = answer.location
            else:
                location = None
            if answer.data == "cmd_pickup":
                is_pickup = True
            else:
                address = answer.text

            # self.bot.send_location(chat_id=self.chat.id, latitude=answer.location.latitude,
            #                        longitude=answer.location.longitude)

    #     # Create an inline keyboard with a single skip button
    #     cancel = telegram.InlineKeyboardMarkup([[telegram.InlineKeyboardButton(self.loc.get("menu_skip"),
    #                                                                            callback_data="cmd_cancel")]])
    #     # Ask if the user wants to add notes to the order
    #     self.bot.send_message(self.chat.id, self.loc.get("ask_order_notes"), reply_markup=cancel)
    #     # Wait for user input
    #     notes = self.__wait_for_regex(r"(.*)", cancellable=True)
    #     # Create a new Order
    #     order = db.Order(user=self.user,
    #                      creation_date=datetime.datetime.now(),
    #                      notes=notes if not isinstance(notes, CancelSignal) else "")
    #     # Add the record to the session and get an ID
    #     self.session.add(order)
    #     self.session.flush()
    #     # For each product added to the cart, create a new OrderItem
    #     for product in cart:
    #         # Create {quantity} new OrderItems
    #         for i in range(0, cart[product][1]):
    #             order_item = db.OrderItem(product=cart[product][0],
    #                                       order_id=order.order_id)
    #             self.session.add(order_item)
    #     # User has credit and valid order, perform transaction now
    #     self.__order_transaction(order=order, value=-int(self.__get_cart_value(cart)))

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
                keyboard.append([self.loc.get("menu_products")])
            if self.admin.receive_orders:
                keyboard.append([self.loc.get("menu_orders")])
            if self.admin.is_owner:
                keyboard.append([self.loc.get("menu_edit_admins")])
            keyboard.append([self.loc.get("menu_user_mode")])
            # Send the previously created keyboard to the user (ensuring it can be clicked only 1 time)
            self.bot.send_message(self.chat.id, self.loc.get("conversation_open_admin_menu"),
                                  reply_markup=telegram.ReplyKeyboardMarkup(keyboard, one_time_keyboard=True))
            # Wait for a reply from the user
            selection = self.__wait_for_specific_message([self.loc.get("menu_products"),
                                                          self.loc.get("menu_orders"),
                                                          self.loc.get("menu_user_mode"),
                                                          self.loc.get("menu_csv"),
                                                          self.loc.get("menu_edit_admins")])
            # If the user has selected the Products option...
            if selection == self.loc.get("menu_products"):
                # Open the products menu
                self.__products_menu()
            # If the user has selected the Orders option...
            elif selection == self.loc.get("menu_orders"):
                # Open the orders menu
                self.__orders_menu()
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
            # If the user has selected the .csv option...
            elif selection == self.loc.get("menu_csv"):
                # Generate the .csv file
                self.__transactions_file()

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
                              reply_markup=telegram.ReplyKeyboardMarkup(keyboard, one_time_keyboard=True))
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
        # Ask for product sizes
        self.bot.send_message(self.chat.id, self.loc.get("ask_product_sizes"))
        children = self.session.query(db.Size).filter_by(product_id=product.id, deleted=False).all()
        if len(children) != 0:
            self.bot.send_message(self.chat.id, self.loc.get("current_sizes",
                                                             sizes_str="\n" \
                                                             .join([child.name + " - " \
                                                                    + str(child.price) \
                                                                    + " " + self.cfg["Payments"]["currency_symbol"]
                                                                    for child in children])),
                                  reply_markup=cancel)
        # Accepts size list in format:
        # 12 [cm, см, сантиметров] - 123456
        sizes = self.__wait_for_regex(r"((((\d{1,2}[ ]{0,2}\w{0,15}( - ){0,1}\d{4,6}\s{0,1}){1,4}|([XxХх]){1})))",
                                      cancellable=bool(product))
        if isinstance(sizes, CancelSignal):
            db_sizes = self.session.query(db.Size).filter_by(product_id=product.id, deleted=False).all()
            sizes = "\n".join([db_size.name + " - " + str(db_size.price) \
                               for db_size in db_sizes])
            price = None
            sizes = sizes.splitlines()
        elif (sizes.lower() == "x") or (sizes.lower() == "х"):
            sizes = None
            print(sizes)
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
                                 deleted=False)
            # Add the record to the database
            self.session.add(product)
            # Flush session to get product id
            self.session.flush()
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

    def __orders_menu(self):
        """Display a live flow of orders."""
        log.debug("Displaying __orders_menu")
        # Create a cancel and a stop keyboard
        stop_keyboard = telegram.InlineKeyboardMarkup([[telegram.InlineKeyboardButton(self.loc.get("menu_stop"),
                                                                                      callback_data="cmd_cancel")]])
        cancel_keyboard = telegram.InlineKeyboardMarkup([[telegram.InlineKeyboardButton(self.loc.get("menu_cancel"),
                                                                                        callback_data="cmd_cancel")]])
        # Send a small intro message on the Live Orders mode
        # Remove the keyboard with the first message... (#39)
        self.bot.send_message(self.chat.id,
                              self.loc.get("conversation_live_orders_start"),
                              reply_markup=telegram.ReplyKeyboardRemove())
        # ...and display a small inline keyboard with the following one
        self.bot.send_message(self.chat.id,
                              self.loc.get("conversation_live_orders_stop"),
                              reply_markup=stop_keyboard)
        # Create the order keyboard
        order_keyboard = telegram.InlineKeyboardMarkup([[telegram.InlineKeyboardButton(self.loc.get("menu_complete"),
                                                                                       callback_data="order_complete")],
                                                        [telegram.InlineKeyboardButton(self.loc.get("menu_refund"),
                                                                                       callback_data="order_refund")]])
        # Display the past pending orders
        orders = self.session.query(db.Order) \
            .filter_by(delivery_date=None, refund_date=None) \
            .join(db.Transaction) \
            .join(db.User) \
            .all()
        # Create a message for every one of them
        for order in orders:
            # Send the created message
            self.bot.send_message(self.chat.id, order.text(w=self, session=self.session),
                                  reply_markup=order_keyboard)
        # Set the Live mode flag to True
        self.admin.live_mode = True
        # Commit the change to the database
        self.session.commit()
        while True:
            # Wait for any message to stop the listening mode
            update = self.__wait_for_inlinekeyboard_callback(cancellable=True)
            # If the user pressed the stop button, exit listening mode
            if isinstance(update, CancelSignal):
                # Stop the listening mode
                self.admin.live_mode = False
                break
            # Find the order
            order_id = re.search(self.loc.get("order_number").replace("{id}", "([0-9]+)"), update.message.text).group(1)
            order = self.session.query(db.Order).filter(db.Order.order_id == order_id).one()
            # Check if the order hasn't been already cleared
            if order.delivery_date is not None or order.refund_date is not None:
                # Notify the admin and skip that order
                self.bot.edit_message_text(self.chat.id, self.loc.get("error_order_already_cleared"))
                break
            # If the user pressed the complete order button, complete the order
            if update.data == "order_complete":
                # Mark the order as complete
                order.delivery_date = datetime.datetime.now()
                # Commit the transaction
                self.session.commit()
                # Update order message
                self.bot.edit_message_text(order.text(w=self, session=self.session), chat_id=self.chat.id,
                                           message_id=update.message.message_id)
                # Notify the user of the completition
                self.bot.send_message(order.user_id,
                                      self.loc.get("notification_order_completed",
                                                   order=order.text(w=self, session=self.session, user=True)))
            # If the user pressed the refund order button, refund the order...
            elif update.data == "order_refund":
                # Ask for a refund reason
                reason_msg = self.bot.send_message(self.chat.id, self.loc.get("ask_refund_reason"),
                                                   reply_markup=cancel_keyboard)
                # Wait for a reply
                reply = self.__wait_for_regex("(.*)", cancellable=True)
                # If the user pressed the cancel button, cancel the refund
                if isinstance(reply, CancelSignal):
                    # Delete the message asking for the refund reason
                    self.bot.delete_message(self.chat.id, reason_msg.message_id)
                    continue
                # Mark the order as refunded
                order.refund_date = datetime.datetime.now()
                # Save the refund reason
                order.refund_reason = reply
                # Refund the credit, reverting the old transaction
                order.transaction.refunded = True
                # Update the order message
                self.bot.edit_message_text(order.text(w=self, session=self.session),
                                           chat_id=self.chat.id,
                                           message_id=update.message.message_id)
                # Notify the user of the refund
                self.bot.send_message(order.user_id,
                                      self.loc.get("notification_order_refunded", order=order.text(w=self,
                                                                                                   session=self.session,
                                                                                                   user=True)))
                # Notify the admin of the refund
                self.bot.send_message(self.chat.id, self.loc.get("success_order_refunded", order_id=order.order_id))

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
                                                    one_time_keyboard=True)
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
                             receive_orders=False,
                             create_transactions=False,
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
                    f"{self.loc.boolmoji(admin.receive_orders)} {self.loc.get('prop_receive_orders')}",
                    callback_data="toggle_receive_orders"
                )],
                [telegram.InlineKeyboardButton(
                    f"{self.loc.boolmoji(admin.create_transactions)} {self.loc.get('prop_create_transactions')}",
                    callback_data="toggle_create_transactions"
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
            elif callback.data == "toggle_receive_orders":
                admin.receive_orders = not admin.receive_orders
            elif callback.data == "toggle_create_transactions":
                admin.create_transactions = not admin.create_transactions
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
