# Copyright 2019 The gRPC Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Tests behavior of the grpc.aio.UnaryUnaryCall class."""

import asyncio
import datetime
import logging
import unittest

import grpc
from grpc.experimental import aio

from src.proto.grpc.testing import messages_pb2, test_pb2_grpc
from tests.unit.framework.common import test_constants
from tests_aio.unit._test_base import AioTestBase
from tests_aio.unit._test_server import start_test_server

_NUM_STREAM_RESPONSES = 5
_RESPONSE_PAYLOAD_SIZE = 42
_REQUEST_PAYLOAD_SIZE = 7
_LOCAL_CANCEL_DETAILS_EXPECTATION = 'Locally cancelled by application!'
_RESPONSE_INTERVAL_US = test_constants.SHORT_TIMEOUT * 1000 * 1000
_UNREACHABLE_TARGET = '0.1:1111'
_INFINITE_INTERVAL_US = 2**31 - 1


class _MulticallableTestMixin():

    async def setUp(self):
        address, self._server = await start_test_server()
        self._channel = aio.insecure_channel(address)
        self._stub = test_pb2_grpc.TestServiceStub(self._channel)

    async def tearDown(self):
        await self._channel.close()
        await self._server.stop(None)


class TestUnaryUnaryCall(_MulticallableTestMixin, AioTestBase):

    async def test_call_ok(self):
        call = self._stub.UnaryCall(messages_pb2.SimpleRequest())

        self.assertFalse(call.done())

        response = await call

        self.assertTrue(call.done())
        self.assertIsInstance(response, messages_pb2.SimpleResponse)
        self.assertEqual(await call.code(), grpc.StatusCode.OK)

        # Response is cached at call object level, reentrance
        # returns again the same response
        response_retry = await call
        self.assertIs(response, response_retry)

    async def test_call_rpc_error(self):
        async with aio.insecure_channel(_UNREACHABLE_TARGET) as channel:
            stub = test_pb2_grpc.TestServiceStub(channel)

            call = stub.UnaryCall(messages_pb2.SimpleRequest(), timeout=0.1)

            with self.assertRaises(grpc.RpcError) as exception_context:
                await call

            self.assertEqual(grpc.StatusCode.DEADLINE_EXCEEDED,
                             exception_context.exception.code())

            self.assertTrue(call.done())
            self.assertEqual(grpc.StatusCode.DEADLINE_EXCEEDED, await
                             call.code())

            # Exception is cached at call object level, reentrance
            # returns again the same exception
            with self.assertRaises(grpc.RpcError) as exception_context_retry:
                await call

            self.assertIs(exception_context.exception,
                          exception_context_retry.exception)

    async def test_call_code_awaitable(self):
        call = self._stub.UnaryCall(messages_pb2.SimpleRequest())
        self.assertEqual(await call.code(), grpc.StatusCode.OK)

    async def test_call_details_awaitable(self):
        call = self._stub.UnaryCall(messages_pb2.SimpleRequest())
        self.assertEqual('', await call.details())

    async def test_call_initial_metadata_awaitable(self):
        call = self._stub.UnaryCall(messages_pb2.SimpleRequest())
        self.assertEqual((), await call.initial_metadata())

    async def test_call_trailing_metadata_awaitable(self):
        call = self._stub.UnaryCall(messages_pb2.SimpleRequest())
        self.assertEqual((), await call.trailing_metadata())

    async def test_cancel_unary_unary(self):
        call = self._stub.UnaryCall(messages_pb2.SimpleRequest())

        self.assertFalse(call.cancelled())

        self.assertTrue(call.cancel())
        self.assertFalse(call.cancel())

        with self.assertRaises(asyncio.CancelledError):
            await call

        # The info in the RpcError should match the info in Call object.
        self.assertTrue(call.cancelled())
        self.assertEqual(await call.code(), grpc.StatusCode.CANCELLED)
        self.assertEqual(await call.details(),
                            'Locally cancelled by application!')

    async def test_cancel_unary_unary_in_task(self):
        coro_started = asyncio.Event()
        call = self._stub.EmptyCall(messages_pb2.SimpleRequest())

        async def another_coro():
            coro_started.set()
            await call

        task = self.loop.create_task(another_coro())
        await coro_started.wait()

        self.assertFalse(task.done())
        task.cancel()

        self.assertEqual(grpc.StatusCode.CANCELLED, await call.code())

        with self.assertRaises(asyncio.CancelledError):
            await task


class TestUnaryStreamCall(_MulticallableTestMixin, AioTestBase):

    async def test_cancel_unary_stream(self):
        # Prepares the request
        request = messages_pb2.StreamingOutputCallRequest()
        for _ in range(_NUM_STREAM_RESPONSES):
            request.response_parameters.append(
                messages_pb2.ResponseParameters(
                    size=_RESPONSE_PAYLOAD_SIZE,
                    interval_us=_RESPONSE_INTERVAL_US,
                ))

        # Invokes the actual RPC
        call = self._stub.StreamingOutputCall(request)
        self.assertFalse(call.cancelled())

        response = await call.read()
        self.assertIs(type(response),
                        messages_pb2.StreamingOutputCallResponse)
        self.assertEqual(_RESPONSE_PAYLOAD_SIZE, len(response.payload.body))

        self.assertTrue(call.cancel())
        self.assertEqual(grpc.StatusCode.CANCELLED, await call.code())
        self.assertEqual(_LOCAL_CANCEL_DETAILS_EXPECTATION, await
                            call.details())
        self.assertFalse(call.cancel())

        with self.assertRaises(asyncio.CancelledError):
            await call.read()
        self.assertTrue(call.cancelled())

    async def test_multiple_cancel_unary_stream(self):
        # Prepares the request
        request = messages_pb2.StreamingOutputCallRequest()
        for _ in range(_NUM_STREAM_RESPONSES):
            request.response_parameters.append(
                messages_pb2.ResponseParameters(
                    size=_RESPONSE_PAYLOAD_SIZE,
                    interval_us=_RESPONSE_INTERVAL_US,
                ))

        # Invokes the actual RPC
        call = self._stub.StreamingOutputCall(request)
        self.assertFalse(call.cancelled())

        response = await call.read()
        self.assertIs(type(response),
                        messages_pb2.StreamingOutputCallResponse)
        self.assertEqual(_RESPONSE_PAYLOAD_SIZE, len(response.payload.body))

        self.assertTrue(call.cancel())
        self.assertFalse(call.cancel())
        self.assertFalse(call.cancel())
        self.assertFalse(call.cancel())

        with self.assertRaises(asyncio.CancelledError):
            await call.read()

    async def test_early_cancel_unary_stream(self):
        """Test cancellation before receiving messages."""
        # Prepares the request
        request = messages_pb2.StreamingOutputCallRequest()
        for _ in range(_NUM_STREAM_RESPONSES):
            request.response_parameters.append(
                messages_pb2.ResponseParameters(
                    size=_RESPONSE_PAYLOAD_SIZE,
                    interval_us=_RESPONSE_INTERVAL_US,
                ))

        # Invokes the actual RPC
        call = self._stub.StreamingOutputCall(request)

        self.assertFalse(call.cancelled())
        self.assertTrue(call.cancel())
        self.assertFalse(call.cancel())

        with self.assertRaises(asyncio.CancelledError):
            await call.read()

        self.assertTrue(call.cancelled())

        self.assertEqual(grpc.StatusCode.CANCELLED, await call.code())
        self.assertEqual(_LOCAL_CANCEL_DETAILS_EXPECTATION, await
                            call.details())

    async def test_late_cancel_unary_stream(self):
        """Test cancellation after received all messages."""
        # Prepares the request
        request = messages_pb2.StreamingOutputCallRequest()
        for _ in range(_NUM_STREAM_RESPONSES):
            request.response_parameters.append(
                messages_pb2.ResponseParameters(
                    size=_RESPONSE_PAYLOAD_SIZE,))

        # Invokes the actual RPC
        call = self._stub.StreamingOutputCall(request)

        for _ in range(_NUM_STREAM_RESPONSES):
            response = await call.read()
            self.assertIs(type(response),
                            messages_pb2.StreamingOutputCallResponse)
            self.assertEqual(_RESPONSE_PAYLOAD_SIZE,
                                len(response.payload.body))

        # After all messages received, it is possible that the final state
        # is received or on its way. It's basically a data race, so our
        # expectation here is do not crash :)
        call.cancel()
        self.assertIn(await call.code(),
                        [grpc.StatusCode.OK, grpc.StatusCode.CANCELLED])

    async def test_too_many_reads_unary_stream(self):
        """Test calling read after received all messages fails."""
        # Prepares the request
        request = messages_pb2.StreamingOutputCallRequest()
        for _ in range(_NUM_STREAM_RESPONSES):
            request.response_parameters.append(
                messages_pb2.ResponseParameters(
                    size=_RESPONSE_PAYLOAD_SIZE,))

        # Invokes the actual RPC
        call = self._stub.StreamingOutputCall(request)

        for _ in range(_NUM_STREAM_RESPONSES):
            response = await call.read()
            self.assertIs(type(response),
                            messages_pb2.StreamingOutputCallResponse)
            self.assertEqual(_RESPONSE_PAYLOAD_SIZE,
                                len(response.payload.body))
        self.assertIs(await call.read(), aio.EOF)

        # After the RPC is finished, further reads will lead to exception.
        self.assertEqual(await call.code(), grpc.StatusCode.OK)
        self.assertIs(await call.read(), aio.EOF)

    async def test_unary_stream_async_generator(self):
        """Sunny day test case for unary_stream."""
        # Prepares the request
        request = messages_pb2.StreamingOutputCallRequest()
        for _ in range(_NUM_STREAM_RESPONSES):
            request.response_parameters.append(
                messages_pb2.ResponseParameters(
                    size=_RESPONSE_PAYLOAD_SIZE,))

        # Invokes the actual RPC
        call = self._stub.StreamingOutputCall(request)
        self.assertFalse(call.cancelled())

        async for response in call:
            self.assertIs(type(response),
                            messages_pb2.StreamingOutputCallResponse)
            self.assertEqual(_RESPONSE_PAYLOAD_SIZE,
                                len(response.payload.body))

        self.assertEqual(await call.code(), grpc.StatusCode.OK)

    async def test_cancel_unary_stream_in_task_using_read(self):
        coro_started = asyncio.Event()

        # Configs the server method to block forever
        request = messages_pb2.StreamingOutputCallRequest()
        request.response_parameters.append(
            messages_pb2.ResponseParameters(
                size=_RESPONSE_PAYLOAD_SIZE,
                interval_us=_INFINITE_INTERVAL_US,
            ))

        # Invokes the actual RPC
        call = self._stub.StreamingOutputCall(request)

        async def another_coro():
            coro_started.set()
            await call.read()

        task = self.loop.create_task(another_coro())
        await coro_started.wait()

        self.assertFalse(task.done())
        task.cancel()

        self.assertEqual(grpc.StatusCode.CANCELLED, await call.code())

        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_cancel_unary_stream_in_task_using_async_for(self):
        coro_started = asyncio.Event()

        # Configs the server method to block forever
        request = messages_pb2.StreamingOutputCallRequest()
        request.response_parameters.append(
            messages_pb2.ResponseParameters(
                size=_RESPONSE_PAYLOAD_SIZE,
                interval_us=_INFINITE_INTERVAL_US,
            ))

        # Invokes the actual RPC
        call = self._stub.StreamingOutputCall(request)

        async def another_coro():
            coro_started.set()
            async for _ in call:
                pass

        task = self.loop.create_task(another_coro())
        await coro_started.wait()

        self.assertFalse(task.done())
        task.cancel()

        self.assertEqual(grpc.StatusCode.CANCELLED, await call.code())

        with self.assertRaises(asyncio.CancelledError):
            await task

    def test_call_credentials(self):

        class DummyAuth(grpc.AuthMetadataPlugin):

            def __call__(self, context, callback):
                signature = context.method_name[::-1]
                callback((("test", signature),), None)

        async def coro():
            server_target, _ = await start_test_server(secure=False)  # pylint: disable=unused-variable

            async with aio.insecure_channel(server_target) as channel:
                hi = channel.unary_unary('/grpc.testing.TestService/UnaryCall',
                                         request_serializer=messages_pb2.
                                         SimpleRequest.SerializeToString,
                                         response_deserializer=messages_pb2.
                                         SimpleResponse.FromString)
                call_credentials = grpc.metadata_call_credentials(DummyAuth())
                call = hi(messages_pb2.SimpleRequest(),
                          credentials=call_credentials)
                response = await call

                self.assertIsInstance(response, messages_pb2.SimpleResponse)
                self.assertEqual(await call.code(), grpc.StatusCode.OK)

        self.loop.run_until_complete(coro())


class TestStreamUnaryCall(_MulticallableTestMixin, AioTestBase):

    async def test_cancel_stream_unary(self):
        call = self._stub.StreamingInputCall()

        # Prepares the request
        payload = messages_pb2.Payload(body=b'\0' * _REQUEST_PAYLOAD_SIZE)
        request = messages_pb2.StreamingInputCallRequest(payload=payload)

        # Sends out requests
        for _ in range(_NUM_STREAM_RESPONSES):
            await call.write(request)

        # Cancels the RPC
        self.assertFalse(call.done())
        self.assertFalse(call.cancelled())
        self.assertTrue(call.cancel())
        self.assertTrue(call.cancelled())

        await call.done_writing()

        with self.assertRaises(asyncio.CancelledError):
            await call

    async def test_early_cancel_stream_unary(self):
        call = self._stub.StreamingInputCall()

        # Cancels the RPC
        self.assertFalse(call.done())
        self.assertFalse(call.cancelled())
        self.assertTrue(call.cancel())
        self.assertTrue(call.cancelled())

        with self.assertRaises(asyncio.InvalidStateError):
            await call.write(messages_pb2.StreamingInputCallRequest())

        # Should be no-op
        await call.done_writing()

        with self.assertRaises(asyncio.CancelledError):
            await call

    async def test_write_after_done_writing(self):
        call = self._stub.StreamingInputCall()

        # Prepares the request
        payload = messages_pb2.Payload(body=b'\0' * _REQUEST_PAYLOAD_SIZE)
        request = messages_pb2.StreamingInputCallRequest(payload=payload)

        # Sends out requests
        for _ in range(_NUM_STREAM_RESPONSES):
            await call.write(request)

        # Should be no-op
        await call.done_writing()

        with self.assertRaises(asyncio.InvalidStateError):
            await call.write(messages_pb2.StreamingInputCallRequest())

        response = await call
        self.assertIsInstance(response, messages_pb2.StreamingInputCallResponse)
        self.assertEqual(_NUM_STREAM_RESPONSES * _REQUEST_PAYLOAD_SIZE,
                         response.aggregated_payload_size)

        self.assertEqual(await call.code(), grpc.StatusCode.OK)

    async def test_error_in_async_generator(self):
        # Server will pause between responses
        request = messages_pb2.StreamingOutputCallRequest()
        request.response_parameters.append(
            messages_pb2.ResponseParameters(
                size=_RESPONSE_PAYLOAD_SIZE,
                interval_us=_RESPONSE_INTERVAL_US,
            ))

        # We expect the request iterator to receive the exception
        request_iterator_received_the_exception = asyncio.Event()

        async def request_iterator():
            with self.assertRaises(asyncio.CancelledError):
                for _ in range(_NUM_STREAM_RESPONSES):
                    yield request
                    await asyncio.sleep(test_constants.SHORT_TIMEOUT)
            request_iterator_received_the_exception.set()

        call = self._stub.StreamingInputCall(request_iterator())

        # Cancel the RPC after at least one response
        async def cancel_later():
            await asyncio.sleep(test_constants.SHORT_TIMEOUT * 2)
            call.cancel()

        cancel_later_task = self.loop.create_task(cancel_later())

        # No exceptions here
        with self.assertRaises(asyncio.CancelledError):
            await call

        await request_iterator_received_the_exception.wait()

        # No failures in the cancel later task!
        await cancel_later_task


# Prepares the request that stream in a ping-pong manner.
_STREAM_OUTPUT_REQUEST_ONE_RESPONSE = messages_pb2.StreamingOutputCallRequest()
_STREAM_OUTPUT_REQUEST_ONE_RESPONSE.response_parameters.append(
    messages_pb2.ResponseParameters(size=_RESPONSE_PAYLOAD_SIZE))


class TestStreamStreamCall(_MulticallableTestMixin, AioTestBase):

    async def test_cancel(self):
        # Invokes the actual RPC
        call = self._stub.FullDuplexCall()

        for _ in range(_NUM_STREAM_RESPONSES):
            await call.write(_STREAM_OUTPUT_REQUEST_ONE_RESPONSE)
            response = await call.read()
            self.assertIsInstance(response,
                                  messages_pb2.StreamingOutputCallResponse)
            self.assertEqual(_RESPONSE_PAYLOAD_SIZE, len(response.payload.body))

        # Cancels the RPC
        self.assertFalse(call.done())
        self.assertFalse(call.cancelled())
        self.assertTrue(call.cancel())
        self.assertTrue(call.cancelled())
        self.assertEqual(grpc.StatusCode.CANCELLED, await call.code())

    async def test_cancel_with_pending_read(self):
        call = self._stub.FullDuplexCall()

        await call.write(_STREAM_OUTPUT_REQUEST_ONE_RESPONSE)

        # Cancels the RPC
        self.assertFalse(call.done())
        self.assertFalse(call.cancelled())
        self.assertTrue(call.cancel())
        self.assertTrue(call.cancelled())
        self.assertEqual(grpc.StatusCode.CANCELLED, await call.code())

    async def test_cancel_with_ongoing_read(self):
        call = self._stub.FullDuplexCall()
        coro_started = asyncio.Event()

        async def read_coro():
            coro_started.set()
            await call.read()

        read_task = self.loop.create_task(read_coro())
        await coro_started.wait()
        self.assertFalse(read_task.done())

        # Cancels the RPC
        self.assertFalse(call.done())
        self.assertFalse(call.cancelled())
        self.assertTrue(call.cancel())
        self.assertTrue(call.cancelled())
        self.assertEqual(grpc.StatusCode.CANCELLED, await call.code())

    async def test_early_cancel(self):
        call = self._stub.FullDuplexCall()

        # Cancels the RPC
        self.assertFalse(call.done())
        self.assertFalse(call.cancelled())
        self.assertTrue(call.cancel())
        self.assertTrue(call.cancelled())
        self.assertEqual(grpc.StatusCode.CANCELLED, await call.code())

    async def test_cancel_after_done_writing(self):
        call = self._stub.FullDuplexCall()
        await call.done_writing()

        # Cancels the RPC
        self.assertFalse(call.done())
        self.assertFalse(call.cancelled())
        self.assertTrue(call.cancel())
        self.assertTrue(call.cancelled())
        self.assertEqual(grpc.StatusCode.CANCELLED, await call.code())

    async def test_late_cancel(self):
        call = self._stub.FullDuplexCall()
        await call.done_writing()
        self.assertEqual(grpc.StatusCode.OK, await call.code())

        # Cancels the RPC
        self.assertTrue(call.done())
        self.assertFalse(call.cancelled())
        self.assertFalse(call.cancel())
        self.assertFalse(call.cancelled())

        # Status is still OK
        self.assertEqual(grpc.StatusCode.OK, await call.code())

    async def test_async_generator(self):

        async def request_generator():
            yield _STREAM_OUTPUT_REQUEST_ONE_RESPONSE
            yield _STREAM_OUTPUT_REQUEST_ONE_RESPONSE

        call = self._stub.FullDuplexCall(request_generator())
        async for response in call:
            self.assertEqual(_RESPONSE_PAYLOAD_SIZE, len(response.payload.body))

        self.assertEqual(await call.code(), grpc.StatusCode.OK)

    async def test_too_many_reads(self):

        async def request_generator():
            for _ in range(_NUM_STREAM_RESPONSES):
                yield _STREAM_OUTPUT_REQUEST_ONE_RESPONSE

        call = self._stub.FullDuplexCall(request_generator())
        for _ in range(_NUM_STREAM_RESPONSES):
            response = await call.read()
            self.assertEqual(_RESPONSE_PAYLOAD_SIZE, len(response.payload.body))
        self.assertIs(await call.read(), aio.EOF)

        self.assertEqual(await call.code(), grpc.StatusCode.OK)
        # After the RPC finished, the read should also produce EOF
        self.assertIs(await call.read(), aio.EOF)

    async def test_read_write_after_done_writing(self):
        call = self._stub.FullDuplexCall()

        # Writes two requests, and pending two requests
        await call.write(_STREAM_OUTPUT_REQUEST_ONE_RESPONSE)
        await call.write(_STREAM_OUTPUT_REQUEST_ONE_RESPONSE)
        await call.done_writing()

        # Further write should fail
        with self.assertRaises(asyncio.InvalidStateError):
            await call.write(_STREAM_OUTPUT_REQUEST_ONE_RESPONSE)

        # But read should be unaffected
        response = await call.read()
        self.assertEqual(_RESPONSE_PAYLOAD_SIZE, len(response.payload.body))
        response = await call.read()
        self.assertEqual(_RESPONSE_PAYLOAD_SIZE, len(response.payload.body))

        self.assertEqual(await call.code(), grpc.StatusCode.OK)

    async def test_error_in_async_generator(self):
        # Server will pause between responses
        request = messages_pb2.StreamingOutputCallRequest()
        request.response_parameters.append(
            messages_pb2.ResponseParameters(
                size=_RESPONSE_PAYLOAD_SIZE,
                interval_us=_RESPONSE_INTERVAL_US,
            ))

        # We expect the request iterator to receive the exception
        request_iterator_received_the_exception = asyncio.Event()

        async def request_iterator():
            with self.assertRaises(asyncio.CancelledError):
                for _ in range(_NUM_STREAM_RESPONSES):
                    yield request
                    await asyncio.sleep(test_constants.SHORT_TIMEOUT)
            request_iterator_received_the_exception.set()

        call = self._stub.FullDuplexCall(request_iterator())

        # Cancel the RPC after at least one response
        async def cancel_later():
            await asyncio.sleep(test_constants.SHORT_TIMEOUT * 2)
            call.cancel()

        cancel_later_task = self.loop.create_task(cancel_later())

        # No exceptions here
        async for response in call:
            self.assertEqual(_RESPONSE_PAYLOAD_SIZE, len(response.payload.body))

        await request_iterator_received_the_exception.wait()

        self.assertEqual(grpc.StatusCode.CANCELLED, await call.code())
        # No failures in the cancel later task!
        await cancel_later_task


if __name__ == '__main__':
    logging.basicConfig()
    unittest.main(verbosity=2)
