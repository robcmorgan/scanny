from datetime import datetime, date
from typing import Optional
from sqlalchemy import Integer, String, DateTime, Date, Text, ForeignKey, Boolean
from sqlalchemy.orm import Mapped, mapped_column, relationship
from database import Base


class Group(Base):
    __tablename__ = "groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    include_in_path: Mapped[bool] = mapped_column(Boolean, default=True)

    subgroups: Mapped[list["Subgroup"]] = relationship("Subgroup", back_populates="group", order_by="Subgroup.name")


class Subgroup(Base):
    __tablename__ = "subgroups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(Integer, ForeignKey("groups.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    filename_tag: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)   # overrides name in filename
    custom_path: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)  # overrides folder location
    use_count: Mapped[int] = mapped_column(Integer, default=0)
    last_used: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    group: Mapped["Group"] = relationship("Group", back_populates="subgroups")
    pending_scans: Mapped[list["PendingScan"]] = relationship("PendingScan", back_populates="subgroup")
    documents: Mapped[list["Document"]] = relationship("Document", back_populates="subgroup")


class PendingScan(Base):
    """Record created on the phone before scanning. Equivalent to FileMaker 'On phone' table."""
    __tablename__ = "pending_scans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subgroup_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("subgroups.id"), nullable=True)
    item_one_off_description: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    modified_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    rob_task: Mapped[bool] = mapped_column(Boolean, default=False)
    beccy_task: Mapped[bool] = mapped_column(Boolean, default=False)
    task_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    comment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    date_override: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    override_filename: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending, filed, error

    subgroup: Mapped[Optional["Subgroup"]] = relationship("Subgroup", back_populates="pending_scans")
    document: Mapped[Optional["Document"]] = relationship("Document", back_populates="pending_scan", uselist=False)


class Document(Base):
    """A filed document. Equivalent to FileMaker 'Files' table."""
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # Drive / file identity
    drive_file_id: Mapped[Optional[str]] = mapped_column(String(255), unique=True, nullable=True)
    file_name_when_imported: Mapped[str] = mapped_column(String(500))
    timestamp_from_filename: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    time_file_was_imported: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # Category assignment (copied at filing time)
    group_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("groups.id"), nullable=True)
    subgroup_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("subgroups.id"), nullable=True)
    pending_scan_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("pending_scans.id"), nullable=True)

    # Fields copied from the phone record at filing time
    assigned_item_one_off_description: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    assigned_comment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    assigned_rob_task: Mapped[bool] = mapped_column(Boolean, default=False)
    assigned_beccy_task: Mapped[bool] = mapped_column(Boolean, default=False)
    assigned_task_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    assigned_date_override: Mapped[Optional[date]] = mapped_column(Date, nullable=True)

    # Filing paths
    desired_folder_location: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    desired_filename: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    desired_full_path: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    current_full_path: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    moved_and_renamed_timestamp: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Status flags
    broken_link: Mapped[bool] = mapped_column(Boolean, default=False)
    conflicted: Mapped[bool] = mapped_column(Boolean, default=False)

    group: Mapped[Optional["Group"]] = relationship("Group")
    subgroup: Mapped[Optional["Subgroup"]] = relationship("Subgroup", back_populates="documents")
    pending_scan: Mapped[Optional["PendingScan"]] = relationship("PendingScan", back_populates="document")
