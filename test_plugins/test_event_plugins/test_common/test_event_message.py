# coding=utf-8
from __future__ import print_function

import json
import mock
import os
import pytest

from event_plugins.common.schedule.time_utils import TimeUtils, AIRFLOW_EVENT_PLUGINS_TIMEZONE
from event_plugins.common.storage.db import get_session, STORAGE_CONF
from event_plugins.common.storage.event_message import EventMessage, EventMessageCRUD
from event_plugins.common.status import DBStatus


TEST_SENSOR_NAME = 'test'
TEST_SOURCE_TYPE = 'kafka'
TEST_TABLE_NAME = STORAGE_CONF.get("Storage", "table_name")


def patch_now(mocker, now):
    mocker.patch.object(TimeUtils, 'get_now', return_value=now)

def db_commit_without_close(session):
    ''' not closing connection here (close in fixture) '''
    try:
        session.commit()
    except:
        session.rollback()
        raise


@pytest.fixture()
def db():
    # use source type:kafka and sensor_name:test for testing
    session = get_session()
    yield EventMessageCRUD(
        source_type=TEST_SOURCE_TYPE,
        sensor_name=TEST_SENSOR_NAME,
        session=session
    )
    # clear all the rows in the table after every test
    session.query(EventMessage).delete()
    db_commit_without_close(session)

    # if postgresql, reset auto increment ID in the table
    db_name = session.get_bind().dialect.name
    if db_name == 'postgresql':
        session.execute("ALTER SEQUENCE {}_id_seq RESTART WITH 1".format(TEST_TABLE_NAME))
        db_commit_without_close(session)
    session.close()

@pytest.fixture()
def msg_list():
    return [
        {'frequency': 'D', 'topic': 'etl-finish', 'db': 'db0', 'table': 'tbl0',
            'partition_values': "{{yyyymm|dt.format(format='%Y%m')}}", 'task_id': "tbla"},
        {'frequency': 'M', 'topic': 'etl-finish', 'db': 'db0', 'table': 'tbl1',
            'partition_values': "", 'task_id': "tblc"}
    ]

@pytest.fixture()
def overwrite_msg_list():
    return [
        {'frequency': 'D', 'topic': 'etl-finish', 'db': 'db0', 'table': 'tbl0',
            'partition_values': "{{yyyymm|dt.format(format='%Y%m')}}", 'task_id': "tblb"},
        {'frequency': 'M', 'topic': 'etl-finish', 'db': 'db0', 'table': 'tbl1',
            'partition_values': "", 'task_id': "tblc"},
        {'frequency': 'D', 'topic': 'job-finish', 'job_name': 'jn1', 'task_id': "job1"},
    ]


class TestEventMessage:

    @pytest.mark.usefixtures("db", "msg_list")
    def test_insert_invalid_source_type(self, db, msg_list):
        db.source_type="not_available_source"
        with pytest.raises(Exception):
            db.initialize(msg_list)

    @pytest.mark.usefixtures("db", "msg_list")
    def test_insert_invalid_frequency(self, db, msg_list):
        msg_list[0]["frequency"] = 'not_valid'
        with pytest.raises(Exception):
            db.initialize(msg_list)

    @pytest.mark.usefixtures("db")
    def test_non_json_wanted_messages(self, db):
        with pytest.raises(Exception):
            db.initialize("non-json string")

    @pytest.mark.usefixtures("db")
    def test_wanted_messages_none(self, db):
        with pytest.raises(Exception):
            db.initialize(None)


class TestEventMessageCRUD:

    @pytest.mark.usefixtures("db", "msg_list")
    def test_initialize_insert(self, db, msg_list):
        '''
            Check if successfully insert three rows in msg_list while db is not exist,
            and if timeout of all message is set
        '''
        db.initialize(msg_list)
        msgs = db.get_sensor_messages()
        assert msgs.count() == 2
        assert msgs.filter(EventMessage.timeout is None).count() == 0

    @pytest.mark.usefixtures("db", "msg_list", "overwrite_msg_list")
    def test_initialize_update(self, db, msg_list, overwrite_msg_list):
        '''
            Check if successfully overwrite three rows in msg_list while db exists
            Note: overwrite_msg_list is different from msg_list,
                check if function remove unused message records and add new message records
        '''
        db.initialize(msg_list)
        msgs = db.get_sensor_messages()
        assert msgs.count() == 2

        # update msg2 to check if update not remove existing value
        msg2 = {'frequency': 'M', 'topic': 'etl-finish', 'db': 'db0', 'table': 'tbl1',
            'partition_values': "", 'task_id': "tblc"}
        msg2_id = None
        for m in msgs:
            if m.msg == json.dumps(msg2, sort_keys=True):
                msg2_id = m.id
        msg2 = msgs.filter(EventMessage.id == msg2_id).first()
        msg2.last_receive_time = TimeUtils().datetime(2019, 6, 13, tzinfo=AIRFLOW_EVENT_PLUGINS_TIMEZONE)

        db.initialize(overwrite_msg_list)
        msgs = db.get_sensor_messages()
        assert msgs.count() == 3
        assert msgs.filter(EventMessage.id == msg2_id).first().last_receive_time == \
                TimeUtils().datetime(2019, 6, 13, tzinfo=AIRFLOW_EVENT_PLUGINS_TIMEZONE)

    @pytest.mark.usefixtures("db", "msg_list")
    def test_get_timeout(self, db, msg_list, mocker):
        patch_now(mocker, TimeUtils().datetime(2019, 6, 5, 8, 0, 0, tzinfo=AIRFLOW_EVENT_PLUGINS_TIMEZONE))
        for msg in msg_list:
            timeout = db.get_timeout(msg)
            if msg['frequency'] == 'D':
                assert timeout == TimeUtils().datetime(2019, 6, 5, 23, 59, 59, tzinfo=AIRFLOW_EVENT_PLUGINS_TIMEZONE)
            elif msg['frequency'] == 'M':
                assert timeout == TimeUtils().datetime(2019, 6, 30, 23, 59, 59, tzinfo=AIRFLOW_EVENT_PLUGINS_TIMEZONE)

    @pytest.mark.usefixtures("db")
    def test_reset_timeout(self, db, mocker):
        '''
            Check if last_receive and last_receive_time are assigned to None
            when time > message's timeout time
        '''
        # mock TimeUtils().get_now()
        patch_now(mocker, TimeUtils().datetime(2019, 6, 16, 0, 0, 0, tzinfo=AIRFLOW_EVENT_PLUGINS_TIMEZONE))
        fake_now = TimeUtils().datetime(2019, 6, 16, 0, 0, 0, tzinfo=AIRFLOW_EVENT_PLUGINS_TIMEZONE)

        # this record is timeout since fake_now > timeout
        msg1 = {"test_memo":"timeout", "frequency":"D", "topic":"etl-finish"}
        last_receive1 = {'test':1}
        record1 = EventMessage(
            name=TEST_SENSOR_NAME,
            msg=msg1,
            source_type=TEST_SOURCE_TYPE,
            frequency=msg1['frequency'],
            last_receive=last_receive1,
            last_receive_time=TimeUtils().datetime(2019, 6, 15, tzinfo=AIRFLOW_EVENT_PLUGINS_TIMEZONE),
            timeout=TimeUtils().datetime(2019, 6, 15, 23, 59, 59, tzinfo=AIRFLOW_EVENT_PLUGINS_TIMEZONE)
        )
        # this second record is not timeout since fake_now < timeout
        msg2 = {"test_memo":"not_timeout", "frequency":"M", "topic":"etl-finish"}
        last_receive2 = {'test':2}
        record2 = EventMessage(
            name=TEST_SENSOR_NAME,
            msg=msg2,
            source_type=TEST_SOURCE_TYPE,
            frequency='M',
            last_receive=last_receive2,
            last_receive_time=TimeUtils().datetime(2019, 6, 13, tzinfo=AIRFLOW_EVENT_PLUGINS_TIMEZONE),
            timeout=TimeUtils().datetime(2019, 6, 30, 23, 59, 59, tzinfo=AIRFLOW_EVENT_PLUGINS_TIMEZONE)
        )
        db.session.add_all([record1, record2])
        db.session.commit()

        db.reset_timeout()
        records = db.get_sensor_messages().all()
        timeout_record = filter(lambda r: r.msg == json.dumps(msg1, sort_keys=True), records)[0]
        assert (
            timeout_record.last_receive is None and
            timeout_record.last_receive_time is None and
            timeout_record.timeout == TimeUtils().datetime(2019, 6, 16, 23, 59, 59, tzinfo=AIRFLOW_EVENT_PLUGINS_TIMEZONE)
        )
        untimeout_record = filter(lambda r: r.msg == json.dumps(msg2, sort_keys=True), records)[0]
        assert (
            untimeout_record.last_receive == json.dumps(last_receive2, sort_keys=True) and
            untimeout_record.last_receive_time == TimeUtils().datetime(2019, 6, 13, tzinfo=AIRFLOW_EVENT_PLUGINS_TIMEZONE)
        )

    @pytest.mark.usefixtures("db")
    def test_get_sensor_messages(self, db):
        assert db.get_sensor_messages().count() == 0

    @pytest.mark.usefixtures("db")
    def test_status(self, db):
        # insert records - emulate second message not receiving yet
        msg1 = {"test_memo":"received", "frequency":"D", "topic":"etl-finish"}
        record1 = EventMessage(
            name=TEST_SENSOR_NAME,
            msg=msg1,
            source_type=TEST_SOURCE_TYPE,
            frequency='D',
            last_receive={'test':1},
            last_receive_time=TimeUtils().datetime(2019, 6, 15, tzinfo=AIRFLOW_EVENT_PLUGINS_TIMEZONE),
            timeout=TimeUtils().datetime(2019, 6, 15, 23, 59, 59, tzinfo=AIRFLOW_EVENT_PLUGINS_TIMEZONE)
        )
        msg2 = {"test_memo":"not_received", "frequency":"M", "topic":"etl-finish"}
        record2 = EventMessage(
            name=TEST_SENSOR_NAME,
            msg=msg2,
            source_type=TEST_SOURCE_TYPE,
            frequency='M',
            last_receive=None,
            last_receive_time=None,
            timeout=TimeUtils().datetime(2019, 6, 30, 23, 59, 59, tzinfo=AIRFLOW_EVENT_PLUGINS_TIMEZONE)
        )
        db.session.add_all([record1, record2])
        db_commit_without_close(db.session)
        assert db.get_sensor_messages().count() == 2
        assert db.status() == DBStatus.NOT_ALL_RECEIVED

        # last_receive_time and last_receive should be both NOT NONE,
        # or the status would be still NOT_ALL_RECEIVED
        records = db.get_sensor_messages()
        msg2_id = None
        for record in records:
            if record.msg == json.dumps(msg2, sort_keys=True):
                msg2_id = record.id
        msg2 = records.filter(EventMessage.id == msg2_id).first()
        msg2.last_receive_time = TimeUtils().datetime(2019, 6, 13, tzinfo=AIRFLOW_EVENT_PLUGINS_TIMEZONE)
        db_commit_without_close(db.session)
        assert db.get_sensor_messages().count() == 2
        assert db.status() == DBStatus.NOT_ALL_RECEIVED

        # change second record to emulate message received
        msg2.last_receive = json.dumps({'test':1})
        db_commit_without_close(db.session)
        assert db.get_sensor_messages().count() == 2
        assert db.status() == DBStatus.ALL_RECEIVED

        # if receive zero value as message, it is RECEIVED when checking status
        # TODO this might change since only json format is valid message so far
        msg2.last_receive = 0
        db_commit_without_close(db.session)
        assert db.status() == DBStatus.ALL_RECEIVED

    @pytest.mark.usefixtures("db")
    def test_get_unreceived_msgs(self, db):
        # insert records - emulate second message not receiving yet
        msg1 = {"test_memo":"have_receive"}
        record1 = EventMessage(
            name=TEST_SENSOR_NAME,
            msg=msg1,
            source_type=TEST_SOURCE_TYPE,
            frequency='D',
            last_receive={'test':1},
            last_receive_time=TimeUtils().datetime(2019, 6, 15, tzinfo=AIRFLOW_EVENT_PLUGINS_TIMEZONE),
            timeout=TimeUtils().datetime(2019, 6, 15, 23, 59, 59, tzinfo=AIRFLOW_EVENT_PLUGINS_TIMEZONE)
        )
        msg2 = {"test_memo":"not_receive"}
        record2 = EventMessage(
            name=TEST_SENSOR_NAME,
            msg=msg2,
            source_type=TEST_SOURCE_TYPE,
            frequency='M',
            last_receive=None,
            last_receive_time=None,
            timeout=TimeUtils().datetime(2019, 6, 30, 23, 59, 59, tzinfo=AIRFLOW_EVENT_PLUGINS_TIMEZONE)
        )
        db.session.add_all([record1, record2])
        db_commit_without_close(db.session)

        # unreceived: last_receive_time and last_receive are both NONE
        unreceived_msg = db.get_unreceived_msgs()[0]
        assert unreceived_msg['test_memo'] == 'not_receive'

    @pytest.mark.usefixtures("db")
    def test_have_successed_msgs(self, db):
        # emulate received message
        received_msgs = [{"test": "received"}]

        msg1 = {"test": "received"}
        record1 = EventMessage(
            name=TEST_SENSOR_NAME,
            msg=msg1,
            source_type=TEST_SOURCE_TYPE,
            frequency='D',
            last_receive={'test':1},
            last_receive_time=TimeUtils().datetime(2019, 6, 15, 1, 0, 0, tzinfo=AIRFLOW_EVENT_PLUGINS_TIMEZONE),
            timeout=TimeUtils().datetime(2019, 6, 15, 23, 59, 59, tzinfo=AIRFLOW_EVENT_PLUGINS_TIMEZONE)
        )
        msg2 = {"test": "have_received"}
        record2 = EventMessage(
            name=TEST_SENSOR_NAME,
            msg=msg2,
            source_type=TEST_SOURCE_TYPE,
            frequency='M',
            last_receive={'test': 2},
            last_receive_time=TimeUtils().datetime(2019, 6, 20, 2, 0, 0, tzinfo=AIRFLOW_EVENT_PLUGINS_TIMEZONE),
            timeout=TimeUtils().datetime(2019, 6, 30, 23, 59, 59, tzinfo=AIRFLOW_EVENT_PLUGINS_TIMEZONE)
        )
        msg3 = {"test": "not_received"}
        record3 = EventMessage(
            name=TEST_SENSOR_NAME,
            msg=msg3,
            source_type=TEST_SOURCE_TYPE,
            frequency='M',
            last_receive=None,
            last_receive_time=None,
            timeout=TimeUtils().datetime(2019, 6, 30, 23, 59, 59, tzinfo=AIRFLOW_EVENT_PLUGINS_TIMEZONE)
        )
        db.session.add_all([record1, record2, record3])
        db_commit_without_close(db.session)

        # have successed: last_receive_time is not None and last_recieve_time < timeout
        have_successed = db.have_successed_msgs(received_msgs)
        assert have_successed == [{"test": "have_received"}]

    @pytest.mark.usefixtures("db")
    def test_update_on_receive(self, db, mocker):
        # mock TimeUtils().get_now()
        patch_now(mocker, TimeUtils().datetime(2019, 6, 15, 14, 0, 0, tzinfo=AIRFLOW_EVENT_PLUGINS_TIMEZONE))
        fake_now = TimeUtils().datetime(2019, 6, 15, 14, 0, 0, tzinfo=AIRFLOW_EVENT_PLUGINS_TIMEZONE)

        msg1 = {"test": "received"}
        record1 = EventMessage(
            name=TEST_SENSOR_NAME,
            msg=msg1,
            source_type=TEST_SOURCE_TYPE,
            frequency='D',
            last_receive=None,
            last_receive_time=None,
            timeout=TimeUtils().datetime(2019, 6, 15, 23, 59, 59, tzinfo=AIRFLOW_EVENT_PLUGINS_TIMEZONE)
        )
        db.session.add(record1)
        db_commit_without_close(db.session)

        db.update_on_receive(msg1, msg1)
        record = db.get_sensor_messages().first()
        assert record.last_receive == json.dumps(msg1, sort_keys=True)
        assert record.last_receive_time == fake_now
        assert record.timeout == TimeUtils().datetime(2019, 6, 15, 23, 59, 59, tzinfo=AIRFLOW_EVENT_PLUGINS_TIMEZONE)

    @pytest.mark.usefixtures("db")
    def test_delete(self, db):
        msg1 = {"test": "received"}
        record1 = EventMessage(
            name=TEST_SENSOR_NAME,
            msg=msg1,
            source_type=TEST_SOURCE_TYPE,
            frequency='D',
            last_receive=None,
            last_receive_time=None,
            timeout=TimeUtils().datetime(2019, 6, 15, 23, 59, 59, tzinfo=AIRFLOW_EVENT_PLUGINS_TIMEZONE)
        )
        db.session.add(record1)
        db_commit_without_close(db.session)
        assert db.get_sensor_messages().count() == 1
        db.delete()
        assert db.get_sensor_messages().count() == 0

    @pytest.mark.usefixtures("db")
    def test_tabluate_data(self, db, capsys):
        #drop_test_table(db.session)
        msg1 = {"test": "received"}
        timeout1 = TimeUtils().datetime(2019, 6, 15, 23, 59, 59, tzinfo=AIRFLOW_EVENT_PLUGINS_TIMEZONE)
        record1 = EventMessage(
            name=TEST_SENSOR_NAME,
            msg=msg1,
            source_type=TEST_SOURCE_TYPE,
            frequency='D',
            last_receive=None,
            last_receive_time=None,
            timeout=timeout1
        )
        msg2 = {"test": "have_received"}
        last_receive_time2 = TimeUtils().datetime(2019, 6, 20, 2, 0, 0, tzinfo=AIRFLOW_EVENT_PLUGINS_TIMEZONE)
        timeout2 = TimeUtils().datetime(2019, 6, 30, 23, 59, 59, tzinfo=AIRFLOW_EVENT_PLUGINS_TIMEZONE)
        record2 = EventMessage(
            name=TEST_SENSOR_NAME,
            msg=msg2,
            source_type=TEST_SOURCE_TYPE,
            frequency='M',
            last_receive={'test': 2},
            last_receive_time=last_receive_time2,
            timeout=timeout2
        )
        db.session.add_all([record1, record2])
        db_commit_without_close(db.session)

        def add_semicolon(d):
            return d[:-2] + ":" + d[-2:]

        str_timeout1 = add_semicolon(TimeUtils().cvt_datetime2str(timeout1, fmt='%Y-%m-%d %H:%M:%S%z'))
        str_timeout2 = add_semicolon(TimeUtils().cvt_datetime2str(timeout2, fmt='%Y-%m-%d %H:%M:%S%z'))
        str_last_receive_time2 = add_semicolon(TimeUtils().cvt_datetime2str(last_receive_time2, fmt='%Y-%m-%d %H:%M:%S%z'))
        result = db.tabulate_data()
        print(result)
        """a'{a}'"""
        captured = capsys.readouterr()
        expected_result = (
"""╒══════╤════════╤═══════════════════════════╤═══════════════╤═════════════╤════════════════╤═══════════════════════════╤═══════════════════════════╕
│   id │ name   │ msg                       │ source_type   │ frequency   │ last_receive   │ last_receive_time         │ timeout                   │
╞══════╪════════╪═══════════════════════════╪═══════════════╪═════════════╪════════════════╪═══════════════════════════╪═══════════════════════════╡
│    1 │ test   │ {"test": "received"}      │ kafka         │ D           │ None           │ None                      │ %s │
├──────┼────────┼───────────────────────────┼───────────────┼─────────────┼────────────────┼───────────────────────────┼───────────────────────────┤
│    2 │ test   │ {"test": "have_received"} │ kafka         │ M           │ {"test": 2}    │ %s │ %s │
╘══════╧════════╧═══════════════════════════╧═══════════════╧═════════════╧════════════════╧═══════════════════════════╧═══════════════════════════╛
""" % (
    str_timeout1,
    str_last_receive_time2,
    str_timeout2
)).decode("utf8")
        assert captured.out == expected_result
