import sqlite3, os, ast, shutil, re
from enum import IntEnum

from .util import EscapeMode, Charset, abbreviate_course_name, abbreviate_course_type

SyncMode = IntEnum("SyncMode", "NoSync Metadata Full")


class Semester:
    def __init__(self, id, name=None, order=None):
        self.id = id
        self.name = name
        self.order = order

    def complete(self):
        return self.id and self.name and self.order


class Course:
    def __init__(self, id, semester=None, number=None, name=None, abbrev=None, type=None,
                 type_abbrev=None, sync=None):
        self.id = id
        self.semester = semester
        self.number = number
        self.name = name
        self.type = type
        self.sync = sync
        self._abbrev = abbrev
        self._type_abbrev = type_abbrev

    @property
    def abbrev(self):
        return self._abbrev if self._abbrev else abbreviate_course_name(self.name)

    @abbrev.setter
    def abbrev(self, value):
        self._abbrev = value

    @property
    def auto_abbrev(self):
        return self._abbrev is None

    @property
    def type_abbrev(self):
        return self._type_abbrev if self._type_abbrev else abbreviate_course_type(self.type)

    @type_abbrev.setter
    def type_abbrev(self, value):
        self._type_abbrev = value

    @property
    def auto_type_abbrev(self):
        return self._type_abbrev is None

    def complete(self):
        return self.id and self.semester and self.number and self.name and self.type and self.sync


class File:
    def __init__(self, id, course=None, course_semester=None, course_name=None, course_abbrev=None,
                 course_type=None, course_type_abbrev=None, path=None, name=None, extension=None,
                 author=None, description=None, description_url=None, remote_date=None, copyrighted=False,
                 local_date=None, version=None):
        self.id = id
        self.course = course
        self.course_semester = course_semester
        self.course_name = course_name
        self._course_abbrev = course_abbrev
        self.course_type = course_type
        self._course_type_abbrev = course_type_abbrev
        self.path = path
        self.name = name
        self.extension = extension
        self.author = author
        self.description = description
        self.description_url = description_url
        self.remote_date = remote_date
        self.copyrighted = copyrighted
        self.local_date = local_date
        self.version = version

    @property
    def course_abbrev(self):
        return self._course_abbrev if self._course_abbrev else abbreviate_course_name(self.name)

    @property
    def course_type_abbrev(self):
        return self._course_type_abbrev if self._course_type_abbrev \
            else abbreviate_course_type(self.course_type)

    def complete(self):
        return self.id and self.course and self.path and self.name and self.remote_date


class Folder:
    def __init__(self, id, name=None, parent=None, course=None):
        self.id = id
        self.name = name
        self.parent = parent
        self.course = course

    def complete(self):
        return self.id and self.name and (self.parent or self.course)


class View:
    def __init__(self, id, name=None, format="{course}/{type}/{short-path}/{name}{ext}",
                 base=None, escape=EscapeMode.Similar, charset=Charset.Unicode):
        self.id = id
        self.name = name
        self.format = format
        self.escape = escape
        self.charset = charset
        self.base = base

    def complete(self):
        return self.id and self.format and self.escape and self.charset


class DatabaseVersionError(Exception):
    pass


class QueryError(Exception):
    pass


class Database:
    schema_version = 12

    def __init__(self, file_name):
        def connect(self):
            self.conn = sqlite3.connect(file_name, detect_types=sqlite3.PARSE_DECLTYPES)

        # Try using the existing db, if the version differs from the internal schema version,
        # delete the database and start over
        connect(self)
        db_version, = self.query("PRAGMA user_version", expected_rows=1)[0]
        if db_version < self.schema_version:
            if db_version in [9, 11]:
                # Disconnect and reconnect to create a backup
                self.conn.close()
                base_name, ext = os.path.splitext(file_name)
                backup_file = "{}.backup-schema{}{}".format(base_name, db_version, ext)
                shutil.copyfile(file_name, backup_file)
                connect(self)

                if db_version < 11:
                    self.query_script_file("migrate-9-11.sql")
                if db_version < 12:
                    self.query_script_file("migrate-11-12.sql")

                print("Migrated database from version {} to {}, backup saved to {}".format(
                    db_version, self.schema_version, backup_file))
            elif db_version != 0:
                print("Could not migrate database. Run \"studip clear-cache\" to reset DB. " \
                      + "This will reset all views.")
                self.conn.close()
                raise DatabaseVersionError()

            self.query("PRAGMA user_version = " + str(self.schema_version), expected_rows=0)

            if db_version == 0:  # Empty database
                # Create all tables, views and triggers
                self.query_script_file("setup.sql")
                self.add_view(View(0, "default"))
        elif db_version > self.schema_version:
            print("The client database was created by a more recent version of studip-client" \
                  + " - please update or run \"studip clear-cache\"")
            self.conn.close()
            raise DatabaseVersionError()

    def query(self, sql, expected_rows=-1, *args, **kwargs):
        cursor = self.conn.cursor()
        if args:
            if not kwargs:
                cursor.execute(sql, tuple(*args))
            else:
                raise ValueError("Pass either positional or keyword arguments")
        elif kwargs:
            try:
                cursor.execute(sql, dict(**kwargs))
            except sqlite3.IntegrityError:
                pass
        else:
            cursor.execute(sql)

        if expected_rows != 0:
            rows = cursor.fetchmany(expected_rows)
            if len(rows) < expected_rows:
                raise QueryError("Expected at least {} rows, got {}".format(
                    expected_rows, len(rows)))
            return rows

    def query_script(self, sql):
        return self.conn.cursor().executescript(sql)

    def query_script_file(self, name):
        script_dir = os.path.join(os.path.dirname(os.path.realpath(__file__)), "sql")
        with open(os.path.join(script_dir, name), "r") as file:
            init_script = file.read()
        return self.query_script(init_script)

    def query_multiple(self, sql, args):
        self.conn.cursor().executemany(sql, args)

    def update_semester_list(self, semesters):
        self.query_multiple("""
                INSERT OR REPLACE INTO semesters (id, name, ord)
                VALUES (:id, :name, :order)
            """, (s.__dict__ for s in semesters))

    def list_courses(self, full=False, select_sync_yes=True, select_sync_metadata_only=True,
                     select_sync_no=True):
        sync_modes = [str(int(enum)) for enable, enum in [(select_sync_yes, SyncMode.Full),
                                                          (select_sync_metadata_only, SyncMode.Metadata),
                                                          (select_sync_no, SyncMode.NoSync)]
                      if enable]

        if full:
            rows = self.query("""
                SELECT c.id, s.name, c.number, c.name, c.abbrev, c.type, c.type_abbrev, c.sync
                FROM courses AS c
                INNER JOIN semesters AS s ON s.id = c.semester
                WHERE c.sync IN ({})
                ORDER BY c.name, c.type;
            """.format(", ".join(sync_modes)))
            return [Course(i, s, n, a, b, t, u, SyncMode(sync))
                    for i, s, n, a, b, t, u, sync in rows]
        else:
            rows = self.query("""
                SELECT id FROM courses
                WHERE sync IN ({})
                ORDER BY name;
            """.format(", ".join(sync_modes)))
            return [id for (id,) in rows]

    def get_course_details(self, course_id):
        rows = self.query("""
                SELECT s.name, c.number, c.name, c.abbrev, c.type, c.type_abbrev, c.sync
                FROM courses AS c
                INNER JOIN semesters AS s ON s.id = c.semester
                WHERE c.id = :id;
            """, id=course_id, expected_rows=1)
        semester, number, name, abbrev, type, type_abbrev, sync = rows[0]
        return Course(id=course_id, semester=semester, number=number, name=name,
                      abbrev=abbrev, type=type, type_abbrev=type_abbrev, sync=SyncMode(sync))

    def add_course(self, course):
        self.query("""
                INSERT INTO courses (id, semester, number, name, abbrev, type, type_abbrev, sync)
                VALUES (:id, (SELECT id FROM semesters WHERE name = :sem), :num, :name, :abbrev,
                    :type, :tabbrev, :sync);
            """, id=course.id, sem=course.semester, num=course.number, name=course.name,
                   abbrev=course._abbrev, type=course.type, tabbrev=course._type_abbrev,
                   sync=int(course.sync), expected_rows=0)

    def update_course(self, course):
        self.query("""
                UPDATE courses
                SET number=:num, name=:name, abbrev=:abbrev, type=:type, type_abbrev=:type_abbrev,
                    sync=:sync
                WHERE id=:id;
            """, id=course.id, num=course.number, name=course.name, abbrev=course.abbrev,
                   type=course.type, type_abbrev=course.type_abbrev, sync=int(course.sync),
                   expected_rows=0)

    def delete_course(self, course):
        self.query("""
                DELETE FROM courses
                WHERE id = :id;
            """, id=course.id, expected_rows=0)

    def list_files(self, full=False, select_sync_yes=True, select_sync_metadata_only=True,
                   select_sync_no=True):
        Mode = SyncMode
        sync_modes = [str(int(enum)) for enable, enum in [(select_sync_yes, SyncMode.Full),
                                                          (select_sync_metadata_only, SyncMode.Metadata),
                                                          (select_sync_no, SyncMode.NoSync)]
                      if enable]

        if full:
            rows = self.query("""
                    SELECT id, course_id, course_semester, course_name, course_abbrev, course_type,
                        course_type_abbrev, path, name, extension, author, description, remote_date,
                        copyrighted, local_date, version
                    FROM file_details
                    WHERE sync IN ({});
                """.format(", ".join(sync_modes)))
            # Path is encoded as the string representation of a python list
            return [File(i, j, s, c, b, o, u, ast.literal_eval(path), n, e, a, d, t, y, l, v)
                    for i, j, s, c, b, o, u, path, n, e, a, d, t, y, l, v in rows]

        else:
            rows = self.query("""
                    SELECT id
                    FROM file_details
                    WHERE sync IN ({});
                """.format(", ".join(sync_modes)))
            return [id for (id,) in rows]

    def create_parent_for_file(self, file):
        rows = self.query("""
                SELECT root FROM courses
                WHERE id = :course
            """, course=file.course)
        parent, = rows[0]

        for folder in file.path:
            def query_subdirectory():
                return self.query("""
                        SELECT id FROM folders
                        WHERE parent = :par AND name = :name
                    """, par=parent, name=folder)

            rows = query_subdirectory()
            if not rows:
                self.query("""
                        INSERT INTO folders (name, parent)
                        VALUES(:name, :par)
                    """, name=folder, par=parent, expected_rows=0)
                rows = query_subdirectory()
            parent, = rows[0]

        return parent

    def add_file(self, file):
        parent = self.create_parent_for_file(file)
        self.query("""
                INSERT INTO files (id, folder, name, extension, author, description, remote_date,
                    copyrighted, local_date, version)
                VALUES (:id, :par, :name, :ext, :auth, :descr, :creat, :copy, :local, 0);
            """, id=file.id, par=parent, name=file.name, ext=file.extension, auth=file.author,
                   descr=file.description, creat=file.remote_date, copy=file.copyrighted,
                   local=file.local_date, expected_rows=0)

    def update_file(self, file):
        parent = self.create_parent_for_file(file)
        self.query("""
                UPDATE files
                SET folder = :par, name = :name, extension = :ext, author = :auth,
                    description = :descr, remote_date = :creat, copyrighted = :copy,
                    local_date = :local, version = version + 1
                WHERE id = :id;
            """, id=file.id, par=parent, name=file.name, ext=file.extension, auth=file.author,
                   descr=file.description, creat=file.remote_date, copy=file.copyrighted,
                   local=file.local_date, expected_rows=0)
        self.query("""
                DELETE FROM checkouts
                WHERE file=:id
            """, id=file.id, expected_rows=0)

    def update_file_local_date(self, file):
        self.query("""
                UPDATE files
                SET local_date = :local
                WHERE id = :id
            """, id=file.id, local=file.local_date, expected_rows=0)

    def list_views(self, full=False):
        if full:
            rows = self.query("""
                    SELECT id, name, format, base, esc_mode, charset
                    FROM views
                    ORDER BY name;
                """)
            return [View(i, n, f, b, EscapeMode(e), Charset(c)) for i, n, f, b, e, c in rows]
        else:
            rows = self.query("""
                    SELECT id
                    FROM views
                    ORDER BY name;
                """)
            return [id for id, in rows]

    def get_view_details(self, id):
        rows = self.query("""
                SELECT name, format, esc_mode, charset
                FROM views
                WHERE id = :id
            """, id=id, expected_rows=1)
        n, f, e, c = rows[0]
        return View(id, n, f, EscapeMode(e), Charset(c))

    def add_view(self, view):
        self.query("""
                INSERT INTO views (id, name, format, base, esc_mode, charset)
                VALUES (:id, :name, :fmt, :base, :esc, :char)
            """, id=view.id, name=view.name, fmt=view.format, base=view.base, esc=view.escape,
                   char=view.charset, expected_rows=0)

    def remove_view(self, id):
        self.query("""
                DELETE FROM views
                WHERE id=:id
            """, id=id, expected_rows=0)

    def list_checkouts(self, view_id):
        rows = self.query("""
                SELECT file FROM checkouts
                WHERE view=:view
            """, view=view_id)
        return [id for id, in rows]

    def add_checkout(self, view_id, file_id):
        self.query("""
                INSERT INTO checkouts (view, file)
                VALUES (:view, :file)
            """, view=view_id, file=file_id, expected_rows=0)

    def reset_checkouts(self, view_id):
        self.query("""
                DELETE FROM checkouts
                WHERE view=:view
            """, view=view_id, expected_rows=0)

    def commit(self):
        self.conn.commit()
