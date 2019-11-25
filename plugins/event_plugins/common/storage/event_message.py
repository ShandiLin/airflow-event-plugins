import os
import json
import pytz
from datetime import datetime
from tabulate import tabulate

from sqlalchemy.orm import validates
from sqlalchemy import Column, Integer, String, DateTime, JSON
from sqlalchemy import and_

from airflow.configuration import conf
from airflow.utils.db import provide_session
from airflow.models.base import Base
from airflow.settings import engine

from event_plugins import factory
from event_plugins.common.status import DBStatus
from event_plugins.common.schedule.time_utils import TimeUtils


class EventMessage(Base):

    __tablename__ = 'event_plugins'

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String)
    msg = Column(JSON)
    source_type = Column(String(32))
    frequency = Column(String(4))
    last_receive = Column(JSON)
    last_receive_time = Column(DateTime(timezone=True))
    timeout = Column(DateTime(timezone=True))

    # available options for fields
    available_frequency = ['D', 'M']
    available_source_type = ['base', 'kafka']  # base option is for testing

    def __init__(self, name, msg, source_type, frequency, last_receive, last_receive_time, timeout):
        '''
            name(string): sensor name, to identify different sensors in airflow
            msg(json_obj): wanted message
            source_type(string): consume source name. e.g., kafka
            frequency(string): string such as 'D' or 'M' to show received frequency of message
            last_receive(json_obj): the last received message
            last_receive_time(datetime): the last time received message in 'last_receive' column
            timeout(datetime): when will the received message time out
        '''
        self.msg = msg
        self.name = name
        self.source_type = source_type
        self.frequency = frequency
        self.last_receive = last_receive
        self.last_receive_time = last_receive_time
        self.timeout = timeout

    @validates('frequency')
    def validate_frequency(self, key, frequency):
        if frequency not in self.available_frequency:
            raise ValueError("frequency should be in " + str(self.available_frequency))
        return frequency

    @validates('source_type')
    def validate_source_type(self, key, source_type):
        if source_type not in self.available_source_type:
            raise ValueError("source_type should be in " + str(self.source_type))
        return source_type


class EventMessageCRUD:

    def __init__(self, source_type, sensor_name):
        self.source_type = source_type
        self.sensor_name = sensor_name
        # create table if not exist
        # Base.metadata.create_all(engine)

    @provide_session
    def initialize(self, msg_list, dt=None, session=None):
        if self.get_sensor_messages(session=session).count() > 0:
            dt = dt or TimeUtils().get_now()
            self.update_msgs(msg_list, session=session)
            self.reset_timeout(base_time=dt, session=session)
        else:
            for msg in msg_list:
                record = EventMessage(
                    name=self.sensor_name,
                    msg=msg,
                    source_type=self.source_type,
                    frequency=msg['frequency'],
                    last_receive=None,
                    last_receive_time=None,
                    timeout=self.get_timeout(msg)
                )
                session.add(record)
            session.commit()

    @provide_session
    def get_sensor_messages(self, session=None):
        ''' get messages of self.sensor_name '''
        records = session.query(EventMessage).filter(EventMessage.name == self.sensor_name)
        return records

    @provide_session
    def update_msgs(self, msg_list, session=None):
        '''Compare msgs in msg_list to msgs in db. If there are msgs only exist in db,
            we assume that user do not need old msg, it would delete msgs in db, and
            insert new msgs which is not in db.
            Args:
                msg_list(list of json object): messages that need to be record in db
        '''
        exist_records = session.query(EventMessage).filter(EventMessage.name == self.sensor_name)
        del_records = exist_records.filter(EventMessage.msg.notin_(msg_list))
        del_records.delete(synchronize_session='fetch')

        new_msgs = list()
        for msg in msg_list:
            if msg not in [r.msg for r in exist_records]:
                new_msgs.append(msg)
        for new_msg in new_msgs:
            record = EventMessage(
                name=self.sensor_name,
                msg=new_msg,
                source_type=self.source_type,
                frequency=new_msg['frequency'],
                last_receive=None,
                last_receive_time=None,
                timeout=self.get_timeout(new_msg)
            )
            session.add(record)
        session.commit()

    @provide_session
    def reset_timeout(self, base_time=None, session=None):
        '''Clear last_receive_time and last_receive if base time > timeout of msgs in db
            Args:
                time(time-aware datetime): base time to handle timeout, use now if not given

            e.g., If frequncy is 'D', the system will expect for new message coming everyday,
            clean the last received information of the message.
            ----- 2019/06/15 -----
            | msg | frequency | last_receive_time        |  last_receive |  timeout  |
            | a   | D         | dt(2019, 6, 15, 9, 0, 0) | 'test'        | dt(2019, 6, 15, 23, 59, 59) |
            ----- 2019/06/16 -----
            | msg | frequency | last_receive_time        |  last_receive |  timeout  |
            | a   | D         | None                     |  None         | dt(2019, 6, 16, 23, 59, 59) |
        '''
        base_time = base_time or TimeUtils().get_now()
        update_records = session.query(EventMessage).filter(
            and_(
                EventMessage.name == self.sensor_name,
                EventMessage.timeout < base_time
            )
        )
        for record in update_records:
            record.last_receive_time = None
            record.last_receive = None
            record.timeout = self.get_timeout(record.msg)
        session.commit()

    def get_timeout(self, msg):
        return factory.plugin_factory(self.source_type) \
                .msg_handler(msg=msg, mtype='wanted').timeout()

    @provide_session
    def status(self, session=None):
        '''Status of self.sensor_name
            Return(define in status.py):
                ALL_RECEIVED: if all messages get last_receive and last_receive_time
                NOT_ALL_RECEIVED: there're messages that haven't gotten received time
        '''
        records = session.query(EventMessage).filter(EventMessage.name == self.sensor_name)
        for r in records:
            if r.last_receive is None or r.last_receive_time is None:
                return DBStatus.NOT_ALL_RECEIVED
        return DBStatus.ALL_RECEIVED

    @provide_session
    def get_unreceived_msgs(self, session=None):
        '''
        Return:
            json object list of not-received messages
        '''
        records = session.query(EventMessage).filter(EventMessage.name == self.sensor_name)
        return [r.msg for r in records if r.last_receive is None and r.last_receive_time is None]

    @provide_session
    def have_successed_msgs(self, received_msgs, session=None):
        '''This function is used to skip messages that have received before
            and not timeout. e.g. monthly source.
            Since monthly messages might received more that once within a month
            This function should be invoked after consuming source messages.
            Args:
                received_msgs(list of messages in json format):
                    messages that received and match from source
            Returns:
                json object list of messages that have received
        '''
        records = session.query(EventMessage).filter(EventMessage.name == self.sensor_name)
        successed_msgs = [r.msg for r in records if r.last_receive_time is not None \
                                            and r.last_receive_time < r.timeout]
        successed_but_not_receive = list()
        for successed in successed_msgs:
            if successed not in received_msgs:
                successed_but_not_receive.append(successed)
        return successed_but_not_receive

    @provide_session
    def update_on_receive(self, match_wanted, receive_msg, session=None):
        ''' Update last receive time and object when receiving wanted message '''
        update_record = session.query(EventMessage).filter(
            and_(
                EventMessage.name == self.sensor_name,
                EventMessage.msg == match_wanted
            )
        )
        for record in update_record:
            record.last_receive_time = TimeUtils().get_now()
            record.last_receive = receive_msg
        session.commit()

    @provide_session
    def delete(self, session=None):
        ''' delete all messages rows of self.sensor_name '''
        session.query(EventMessage) \
            .filter(EventMessage.name == self.sensor_name) \
            .delete(synchronize_session='fetch')

    @provide_session
    def tabulate_data(self, threshold=None, tablefmt='fancy_grid', session=None):
        headers = [str(c).split('.')[1] for c in EventMessage.__table__.columns]
        data = list()
        records = session.query(EventMessage).filter(EventMessage.name == self.sensor_name)
        for r in records:
            rows = list()
            for col in headers:
                str_val = str(getattr(r, col))
                if threshold:
                    rows.append(str_val if len(str_val) <= threshold else str_val[:threshold] + '...')
                else:
                    rows.append(str_val)
            data.append(rows)
        return tabulate(data, headers=headers, tablefmt=tablefmt)