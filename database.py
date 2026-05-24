"""
Database models, session management, and auto-migration for Reputation Bot.
"""

import sqlite3
import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    TypeDecorator,
    create_engine,
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

logger = logging.getLogger(__name__)

class SafeDateTime(TypeDecorator):
    """Custom DateTime type decorator that gracefully handles invalid date formats in SQLite."""
    impl = String
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.isoformat()
        return str(value)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, datetime):
            return value
        if isinstance(value, str):
            try:
                # Try standard parsing (handles isoformat)
                return datetime.fromisoformat(value)
            except ValueError:
                # Try fallback standard formats just in case
                for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                    try:
                        return datetime.strptime(value, fmt)
                    except ValueError:
                        continue
                logger.warning(f"Failed to parse datetime string: {value!r} - returning None")
                return None
        return value


Base = declarative_base()

# ═══════════════════════════════════════════════════════════════════════════════
#  MODELS
# ═══════════════════════════════════════════════════════════════════════════════


class User(Base):
    __tablename__ = "users"

    id = Column(BigInteger, primary_key=True)       # Telegram User ID (static)
    username = Column(String, nullable=True)          # @username (can change)
    first_name = Column(String, nullable=True)
    last_name = Column(String, nullable=True)
    vouches = Column(Integer, default=0)              # Net reputation score
    messages_count = Column(Integer, default=0)       # Total messages seen

    # Flags
    is_flagged = Column(Integer, default=0)           # 0=Clean, 1=Flagged
    flag_reason = Column(String, nullable=True)
    is_sex_worker = Column(Integer, default=0)        # 0=Clean, 1=Sex Worker
    is_dangerous = Column(Integer, default=0)         # 0=Clean, 1=DANGEROUS
    in_gatekeeper = Column(Integer, default=0)        # 0=Out, 1=In

    # Tracking
    first_seen = Column(SafeDateTime, default=lambda: datetime.now(timezone.utc))
    last_seen = Column(SafeDateTime, default=lambda: datetime.now(timezone.utc))

    # Relationships
    received_vouches = relationship(
        "Vouch", back_populates="recipient", foreign_keys="Vouch.recipient_id"
    )
    given_vouches = relationship(
        "Vouch", back_populates="voucher", foreign_keys="Vouch.voucher_id"
    )

    def __repr__(self):
        return (
            f"<User(id={self.id}, username='{self.username}', "
            f"vouches={self.vouches}, flagged={self.is_flagged})>"
        )


class Vouch(Base):
    __tablename__ = "vouches"

    id = Column(Integer, primary_key=True, autoincrement=True)
    voucher_id = Column(BigInteger, ForeignKey("users.id"))
    recipient_id = Column(BigInteger, ForeignKey("users.id"))
    value = Column(Integer, default=1)                # +1 or -1
    message_content = Column(String, nullable=True)
    timestamp = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    chat_id = Column(BigInteger, nullable=True)       # Group where vouch happened
    verified = Column(Integer, default=0)             # 0=Pending, 1=Approved, -1=Rejected
    is_sentiment = Column(Integer, default=0)         # 0=Regular Vouch, 1=Sentiment-based Vouch

    voucher = relationship("User", foreign_keys=[voucher_id], back_populates="given_vouches")
    recipient = relationship("User", foreign_keys=[recipient_id], back_populates="received_vouches")

    # Indexes for common queries
    __table_args__ = (
        Index("ix_vouches_voucher_id", "voucher_id"),
        Index("ix_vouches_recipient_id", "recipient_id"),
        Index("ix_vouches_verified", "verified"),
        Index("ix_vouches_is_sentiment", "is_sentiment"),
        Index("ix_vouches_timestamp", "timestamp"),
    )

    def __repr__(self):
        return (
            f"<Vouch(id={self.id}, from={self.voucher_id}, "
            f"to={self.recipient_id}, val={self.value}, verified={self.verified}, is_sentiment={self.is_sentiment})>"
        )


class LongMessage(Base):
    """Tracks messages >1500 chars for scam detection."""
    __tablename__ = "long_messages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("users.id"))
    chat_id = Column(BigInteger, nullable=True)
    timestamp = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    length = Column(Integer)

    __table_args__ = (
        Index("ix_longmsg_user_chat_ts", "user_id", "chat_id", "timestamp"),
    )


class SexWorkerTrigger(Base):
    """Tracks potential sex worker triggers ('keen' < 50 chars, 'nudes')."""
    __tablename__ = "sex_worker_triggers"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("users.id"))
    chat_id = Column(BigInteger, nullable=True)
    timestamp = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    trigger_word = Column(String)

    __table_args__ = (
        Index("ix_swtrig_user_chat_ts", "user_id", "chat_id", "timestamp"),
    )


class PolicyViolation(Base):
    """Tracks blacklist violations for auto-scammer detection (persistent)."""
    __tablename__ = "policy_violations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("users.id"))
    chat_id = Column(BigInteger, nullable=True)
    timestamp = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    term = Column(String, nullable=True)  # The blacklisted term that was matched

    __table_args__ = (
        Index("ix_pv_user_ts", "user_id", "timestamp"),
    )


class OldVouch(Base):
    """Legacy vouches imported from old databases (txt/xml)."""
    __tablename__ = "old_vouches"

    id = Column(Integer, primary_key=True, autoincrement=True)
    target_username = Column(String, nullable=True)
    target_id = Column(BigInteger, nullable=True)
    voucher_username = Column(String, nullable=True)
    voucher_id = Column(BigInteger, nullable=True)
    voucher_name = Column(String, nullable=True)
    raw_text = Column(String, nullable=False)
    value = Column(Integer, default=1)
    timestamp = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    source_file = Column(String, nullable=True)

    __table_args__ = (
        Index("ix_oldvouch_target_username", "target_username"),
        Index("ix_oldvouch_target_id", "target_id"),
    )

    def __repr__(self):
        return f"<OldVouch(id={self.id}, target=@{self.target_username}, text={self.raw_text[:40]})>"


class BotSetting(Base):
    """Persistent key/value settings (e.g. feature toggles)."""
    __tablename__ = "bot_settings"
    key = Column(String, primary_key=True)
    value = Column(String, nullable=True)


class BotMessage(Base):
    """Editable user-facing message templates."""
    __tablename__ = "bot_messages"
    key = Column(String, primary_key=True)
    label = Column(String, nullable=True)   # Human-readable name shown in admin panel
    text = Column(Text, nullable=False)
    variables = Column(String, nullable=True)  # Comma-separated list of available {vars}


class NotificationSubscriber(Base):
    """Tracks users subscribed to newsletter/significant event alerts (/notify)."""
    __tablename__ = "notification_subscribers"
    user_id = Column(BigInteger, primary_key=True)       # Telegram User ID (static)
    username = Column(String, nullable=True)             # @username (can change)
    first_name = Column(String, nullable=True)
    subscribed_at = Column(SafeDateTime, default=lambda: datetime.now(timezone.utc))

    def __repr__(self):
        return f"<NotificationSubscriber(user_id={self.user_id}, username='{self.username}')>"


class BlacklistUser(Base):
    """Tracks permanently blacklisted/banned users."""
    __tablename__ = "blacklist_users"
    user_id = Column(BigInteger, primary_key=True)
    username = Column(String, nullable=True)
    reason = Column(String, nullable=True)
    banned_by = Column(String, nullable=True)
    banned_at = Column(SafeDateTime, default=lambda: datetime.now(timezone.utc))

    def __repr__(self):
        return f"<BlacklistUser(user_id={self.user_id}, username='{self.username}')>"


# ═══════════════════════════════════════════════════════════════════════════════
#  DATABASE SETUP
# ═══════════════════════════════════════════════════════════════════════════════

# Use absolute path based on environment variable or fallback
import os
from sqlalchemy import event

_env_db_path = os.getenv("SHARED_DB_PATH")
if _env_db_path:
    _DB_PATH = Path(_env_db_path)
else:
    _DB_DIR = Path(__file__).parent.resolve()
    _DB_PATH = _DB_DIR / "bot_database.db"

# Ensure target parent directory exists
try:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
except Exception:
    pass

DATABASE_URL = f"sqlite:///{_DB_PATH}"

engine = create_engine(DATABASE_URL, connect_args={"timeout": 5.0}, echo=False)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

@event.listens_for(engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


@contextmanager
def get_session():
    """Context manager for database sessions. Auto-commits on success, rolls back on error."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# ─── Default message templates ───────────────────────────────────────────────
_DEFAULT_MESSAGES = [
    (
        "msg_start",
        "Welcome Message (/start)",
        "\u2b50 **Welcome to Vouch Bot, {first_name}!**\n"
        "{divider}\n\n"
        "\U0001f6e1\ufe0f **What is Vouch Bot?**\n"
        "A permanent reputation system built on trust. We track verified vouches "
        "using permanent Telegram User IDs \u2014 not usernames, which anyone can change.\n\n"
        "\U0001f3db\ufe0f **45,000+ Vouches Imported**\n"
        "All legitimate vouches from **2020\u20132023** have been verified and imported "
        "into our database. If you had an established reputation, it's already here.\n\n"
        "\U0001f512 **Permanent & Redundant**\n"
        "Channels and communities come and go \u2014 your reputation shouldn't. "
        "Vouch Bot is a standalone, backed-up database that follows you everywhere.\n\n"
        "\u2705 **How To Vouch:**\n"
        "\u2022 Reply to their message: `+vouch Great trader!`\n"
        "\u2022 By username: `+vouch @username Fast delivery`\n"
        "\u2022 By user ID: `+vouch 123456789 Legit`\n"
        "\u2022 Quick: `+1`, `vouch+`, `rep+`\n\n"
        "\u274c **Negative Vouch (requires reason):**\n"
        "\u2022 `-vouch Didn't deliver, kept my money`\n\n"
        "\U0001f50d **Check Reputation:**\n"
        "\u2022 `/check` \u2014 your own stats\n"
        "\u2022 `/check @username` \u2014 by username\n"
        "\u2022 `/check 123456789` \u2014 by user ID\n"
        "\u2022 Reply `/check` to someone's message\n\n"
        "\u26a0\ufe0f **Rules:**\n"
        "\u2022 2 vouches max per 24h \u2022 36h cooldown per user\n"
        "\u2022 48h minimum account age to vouch\n"
        "\u2022 All vouches **manually verified** by the mod team\n\n"
        "\u26d4 **ZERO TOLERANCE FOR ILLEGAL/DRUG TERMS**\n"
        "**You MUST NOT use drug names, illegal terminology, weapons, or fraud terms in your vouches!** "
        "The system strictly prohibits them to ensure safety. **You gain absolutely nothing from adding illegal terms.** "
        "Just saying _\"stuff was good, on time, would deal with again\"_ is **perfect and preferred**.\n"
        "**Violations will trigger an instant vouch rejection + policy warning. Repeated violations will be reviewed by the mod team.**\n\n"
        "\u2139\ufe0f _Vouch Bot is an independent tool \u2014 not affiliated with any community._",
        "first_name, divider",
    ),
    (
        "msg_help",
        "Help Message (/help)",
        "\U0001f4d6 **Commands:**\n\n"
        "`/start` \u2014 Full tutorial\n"
        "`/check` \u2014 Your stats (or reply / provide @username / ID)\n"
        "`/help` \u2014 This message\n"
        "`/mydata` \u2014 Export all your data as JSON\n\n"
        "**Vouching:** Reply with `+vouch` or `-vouch`\n"
        "**Lookup:** `/check @username` or `/check 123456789`\n\n"
        "\U0001f3db\ufe0f 45,000+ vouches (2020\u20132023) permanently stored.\n"
        "\u26d4 **ZERO TOLERANCE: Drug/illegal terms are strictly banned in vouches.** "
        "Adding illegal terms provides absolutely no benefit. Keep comments clean: e.g., _\"on time, great deal, would deal with again\"_.\n"
        "Vouches are manually verified every 24\u201348hrs.\n"
        "_Vouch Bot is independent \u2014 your reputation is permanent._",
        "",
    ),
    (
        "msg_vouch_success",
        "Vouch Recorded Confirmation",
        "{icon} **Vouch Recorded!**\n"
        "{divider}\n"
        "\U0001f194 Vouch ID: `{vouch_id}`\n"
        "\U0001f464 **Voucher:** {voucher_name} (`{voucher_id}`)\n"
        "\U0001f3af **Recipient:** {recipient_name} (`{recipient_id}`)\n"
        "\U0001f4ac **Comment:** _{comment}_\n"
        "[🚪Vouch databased/logged in here](https://t.me/VouchLoggerAU)\n"
        "\U0001f4c5 {timestamp}",
        "icon, vouch_id, voucher_name, voucher_id, recipient_name, recipient_id, comment, value_str, action, new_total, timestamp, divider",
    ),
    (
        "msg_blacklist_rejection",
        "Blacklist/Policy Violation Rejection",
        "\u26d4 **VOUCH REJECTED \u2014 POLICY VIOLATION**\n\n"
        "Your message contains terms related to **illegal activity** "
        "(drugs, weapons, fraud, etc).\n\n"
        "This system is for **legitimate reputation** tracking only. "
        "This incident has been **logged and reported** to the moderation team.\n\n"
        "_Repeated violations will result in a permanent ban._",
        "",
    ),
    (
        "msg_sentiment_footer",
        "Auto-Detected Vouch Footer Tip",
        "_Tip: Use `+vouch` or `-vouch` for explicit vouches_",
        "",
    ),
    (
        "msg_dangerous_check",
        "Dangerous User /check Warning",
        "\U0001f6ab **DANGEROUS USER** \U0001f6ab\n\n"
        "\U0001f464 **Name:** {name}\n"
        "\U0001f194 **ID:** `{user_id}`\n\n"
        "\u26a0\ufe0f **WARNING: DO NOT TRADE**\n"
        "This user has been marked as **dangerous/roller**.\n"
        "Avoid all interactions.\n\n"
        "Reason: _{reason}_",
        "name, user_id, reason",
    ),
]

# ─── Default settings ─────────────────────────────────────────────────────────
_DEFAULT_SETTINGS = [
    ("sentiment_enabled", "1"),
    ("welcome_delete_timer", "600"),
    ("kick_delete_timer", "300"),
    ("ban_delete_timer", "600"),
]


def init_db():
    """Create all tables if they don't exist, and auto-migrate schema for upgrades."""
    Base.metadata.create_all(bind=engine)

    # Auto-migrate: add columns that may be missing from older DB versions
    db_path = str(_DB_PATH)
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        # Vouches table migrations
        cursor.execute("PRAGMA table_info(vouches)")
        vouch_columns = [row[1] for row in cursor.fetchall()]
        if "verified" not in vouch_columns:
            cursor.execute("ALTER TABLE vouches ADD COLUMN verified INTEGER DEFAULT 0")
            conn.commit()
            logger.info("Migration: added 'verified' column to vouches table.")

        if "is_sentiment" not in vouch_columns:
            cursor.execute("ALTER TABLE vouches ADD COLUMN is_sentiment INTEGER DEFAULT 0")
            conn.commit()
            logger.info("Migration: added 'is_sentiment' column to vouches table.")

        # Users table migrations
        cursor.execute("PRAGMA table_info(users)")
        user_columns = [row[1] for row in cursor.fetchall()]
        columns_to_check = [
            ("is_sex_worker", "INTEGER DEFAULT 0"),
            ("is_dangerous", "INTEGER DEFAULT 0"),
            ("is_verified", "INTEGER DEFAULT 0"),
            ("vouched_by", "INTEGER DEFAULT NULL"),
            ("vouch_count", "INTEGER DEFAULT 0"),
            ("join_date", "TIMESTAMP DEFAULT NULL"),
            ("status", "TEXT DEFAULT 'active'"),
            ("kick_count", "INTEGER DEFAULT 0"),
            ("in_gatekeeper", "INTEGER DEFAULT 0"),
        ]
        for col_name, col_def in columns_to_check:
            if col_name not in user_columns:
                cursor.execute(f"ALTER TABLE users ADD COLUMN {col_name} {col_def}")
                conn.commit()
                logger.info(f"Migration: added '{col_name}' column to users table.")

        # Clean up any non-iso datetimes in the SQLite database to prevent SQLAlchemy parsing errors
        for col in ["last_seen", "first_seen"]:
            try:
                cursor.execute(f"SELECT id, {col} FROM users WHERE {col} IS NOT NULL")
                rows = cursor.fetchall()
                for r_id, val in rows:
                    if not val:
                        continue
                    try:
                        # Test standard parsing
                        datetime.fromisoformat(val)
                    except ValueError:
                        # Try parsing fallback formats
                        is_valid = False
                        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                            try:
                                datetime.strptime(val, fmt)
                                is_valid = True
                                break
                            except ValueError:
                                continue
                        if not is_valid:
                            # Not a valid date format! Update it to NULL
                            cursor.execute(f"UPDATE users SET {col} = NULL WHERE id = ?", (r_id,))
                conn.commit()
            except Exception as e:
                logger.warning(f"Self-healing check on {col} failed: {e}")

        conn.close()
    except Exception as e:
        logger.warning(f"Migration check failed: {e}")

    # Seed default messages (INSERT OR IGNORE — never overwrites admin edits)
    with SessionLocal() as sess:
        for key, label, text, variables in _DEFAULT_MESSAGES:
            if not sess.query(BotMessage).filter(BotMessage.key == key).first():
                sess.add(BotMessage(key=key, label=label, text=text, variables=variables))
        for key, value in _DEFAULT_SETTINGS:
            if not sess.query(BotSetting).filter(BotSetting.key == key).first():
                sess.add(BotSetting(key=key, value=value))
        
        # Force update msg_vouch_success to the latest format to apply link update immediately
        v_row = sess.query(BotMessage).filter(BotMessage.key == "msg_vouch_success").first()
        if v_row:
            for key, label, text, variables in _DEFAULT_MESSAGES:
                if key == "msg_vouch_success":
                    v_row.text = text
                    break
        sess.commit()


def get_setting(key: str, default: str = "") -> str:
    """Read a bot setting from DB."""
    with SessionLocal() as sess:
        row = sess.query(BotSetting).filter(BotSetting.key == key).first()
        return row.value if row else default


def set_setting(key: str, value: str) -> None:
    """Write a bot setting to DB."""
    with SessionLocal() as sess:
        row = sess.query(BotSetting).filter(BotSetting.key == key).first()
        if row:
            row.value = value
        else:
            sess.add(BotSetting(key=key, value=value))
        sess.commit()


def get_bot_message(key: str, **kwargs) -> str:
    """Fetch an editable message template from DB and format it with kwargs."""
    with SessionLocal() as sess:
        row = sess.query(BotMessage).filter(BotMessage.key == key).first()
        template = row.text if row else f"[missing message: {key}]"
    try:
        return template.format(**kwargs) if kwargs else template
    except KeyError:
        return template  # Return raw template if format fails


def get_all_messages() -> list:
    """Return all BotMessage rows as dicts for the admin panel."""
    with SessionLocal() as sess:
        rows = sess.query(BotMessage).order_by(BotMessage.key).all()
        return [
            {"key": r.key, "label": r.label, "text": r.text, "variables": r.variables}
            for r in rows
        ]


def set_bot_message(key: str, text: str) -> bool:
    """Update a message template. Returns True if the key existed."""
    with SessionLocal() as sess:
        row = sess.query(BotMessage).filter(BotMessage.key == key).first()
        if not row:
            return False
        row.text = text
        sess.commit()
        return True


# ─── Subscription Helpers ──────────────────────────────────────────────────

def add_notification_subscriber(user_id: int, username: str, first_name: str) -> bool:
    """Add a user to the notification list. Returns True if successfully subscribed."""
    with SessionLocal() as sess:
        sub = sess.query(NotificationSubscriber).filter(NotificationSubscriber.user_id == user_id).first()
        if sub:
            sub.username = username
            sub.first_name = first_name
            sess.commit()
            return False  # Already subscribed
        sess.add(NotificationSubscriber(user_id=user_id, username=username, first_name=first_name))
        sess.commit()
        return True

def remove_notification_subscriber(user_id: int) -> bool:
    """Remove a user from the notification list. Returns True if successfully unsubscribed."""
    with SessionLocal() as sess:
        sub = sess.query(NotificationSubscriber).filter(NotificationSubscriber.user_id == user_id).first()
        if sub:
            sess.delete(sub)
            sess.commit()
            return True
        return False

def is_notification_subscribed(user_id: int) -> bool:
    """Check if a user is subscribed to notifications."""
    with SessionLocal() as sess:
        sub = sess.query(NotificationSubscriber).filter(NotificationSubscriber.user_id == user_id).first()
        return sub is not None

def get_notification_subscribers() -> list[int]:
    """Get all subscribed user IDs."""
    with SessionLocal() as sess:
        rows = sess.query(NotificationSubscriber.user_id).all()
        return [r[0] for r in rows]


# ─── Blacklist Helpers ───────────────────────────────────────────────────────

def blacklist_user(user_id: int, username: str | None, reason: str, banned_by: str) -> None:
    """Add or update a user on the permanent blacklist database."""
    with SessionLocal() as sess:
        row = sess.query(BlacklistUser).filter(BlacklistUser.user_id == user_id).first()
        if not row:
            sess.add(BlacklistUser(user_id=user_id, username=username, reason=reason, banned_by=banned_by))
        else:
            row.username = username
            row.reason = reason
            row.banned_by = banned_by
        sess.commit()

def is_blacklisted_user(user_id: int) -> bool:
    """Check if a user ID is permanently blacklisted."""
    with SessionLocal() as sess:
        row = sess.query(BlacklistUser).filter(BlacklistUser.user_id == user_id).first()
        return row is not None

