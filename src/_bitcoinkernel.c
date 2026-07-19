/*
 * _bitcoinkernel.c - CPython bindings for Bitcoin Core's libbitcoinkernel.
 *
 * Wraps the C API declared in <bitcoinkernel.h> using the Python/C API.
 *
 * Ownership model:
 *   - Every wrapper object owns the btck_* pointer it holds and destroys it
 *     in tp_dealloc, except BlockTreeEntry and Chain, which are unowned
 *     views into the chainstate manager. Those hold a strong reference to
 *     the owning ChainstateManager object to keep the underlying memory
 *     alive.
 *   - Functions returning unowned const views (e.g. a transaction's output)
 *     copy the view immediately with the corresponding btck_*_copy function,
 *     which is cheap (usually a reference count increment inside the kernel
 *     library).
 *
 * Threading:
 *   - Long-running kernel calls (chainstate manager creation/destruction,
 *     block processing, imports, disk reads) release the GIL.
 *   - Kernel callbacks (logging, notifications, validation interface) may
 *     arrive on arbitrary kernel threads and re-acquire the GIL through
 *     PyGILState_Ensure before touching Python objects.
 */

#define PY_SSIZE_T_CLEAN
#include <Python.h>

#include <bitcoinkernel.h>

#include <stdint.h>
#include <stdlib.h>
#include <string.h>

/* ------------------------------------------------------------------ */
/* Object structs                                                     */
/* ------------------------------------------------------------------ */

typedef struct {
    PyObject_HEAD
    btck_Transaction* ptr;
} TransactionObject;

typedef struct {
    PyObject_HEAD
    btck_ScriptPubkey* ptr;
} ScriptPubkeyObject;

typedef struct {
    PyObject_HEAD
    btck_TransactionOutput* ptr;
} TransactionOutputObject;

typedef struct {
    PyObject_HEAD
    btck_TransactionInput* ptr;
} TransactionInputObject;

typedef struct {
    PyObject_HEAD
    btck_TransactionOutPoint* ptr;
} TransactionOutPointObject;

typedef struct {
    PyObject_HEAD
    btck_Txid* ptr;
} TxidObject;

typedef struct {
    PyObject_HEAD
    btck_PrecomputedTransactionData* ptr;
} PrecomputedTransactionDataObject;

typedef struct {
    PyObject_HEAD
    btck_BlockHash* ptr;
} BlockHashObject;

typedef struct {
    PyObject_HEAD
    btck_BlockHeader* ptr;
} BlockHeaderObject;

typedef struct {
    PyObject_HEAD
    btck_Block* ptr;
} BlockObject;

typedef struct {
    PyObject_HEAD
    btck_BlockValidationState* ptr;
} BlockValidationStateObject;

typedef struct {
    PyObject_HEAD
    btck_LoggingConnection* ptr;
} LoggingConnectionObject;

typedef struct {
    PyObject_HEAD
    btck_ChainParameters* ptr;
} ChainParametersObject;

typedef struct {
    PyObject_HEAD
    btck_ContextOptions* ptr;
} ContextOptionsObject;

typedef struct {
    PyObject_HEAD
    btck_Context* ptr;
} ContextObject;

typedef struct {
    PyObject_HEAD
    btck_ChainstateManagerOptions* ptr;
    PyObject* context; /* keep the Context wrapper alive */
} ChainstateManagerOptionsObject;

typedef struct {
    PyObject_HEAD
    btck_ChainstateManager* ptr;
    PyObject* context; /* keep the Context wrapper alive */
} ChainstateManagerObject;

typedef struct {
    PyObject_HEAD
    const btck_BlockTreeEntry* ptr; /* unowned view */
    PyObject* owner;                /* ChainstateManager or NULL (callback context) */
} BlockTreeEntryObject;

typedef struct {
    PyObject_HEAD
    const btck_Chain* ptr; /* unowned view */
    PyObject* owner;       /* ChainstateManager */
} ChainObject;

typedef struct {
    PyObject_HEAD
    btck_BlockSpentOutputs* ptr;
} BlockSpentOutputsObject;

typedef struct {
    PyObject_HEAD
    btck_TransactionSpentOutputs* ptr;
} TransactionSpentOutputsObject;

typedef struct {
    PyObject_HEAD
    btck_Coin* ptr;
} CoinObject;

/* Forward type declarations */
static PyTypeObject TransactionType;
static PyTypeObject ScriptPubkeyType;
static PyTypeObject TransactionOutputType;
static PyTypeObject TransactionInputType;
static PyTypeObject TransactionOutPointType;
static PyTypeObject TxidType;
static PyTypeObject PrecomputedTransactionDataType;
static PyTypeObject BlockHashType;
static PyTypeObject BlockHeaderType;
static PyTypeObject BlockType;
static PyTypeObject BlockValidationStateType;
static PyTypeObject LoggingConnectionType;
static PyTypeObject ChainParametersType;
static PyTypeObject ContextOptionsType;
static PyTypeObject ContextType;
static PyTypeObject ChainstateManagerOptionsType;
static PyTypeObject ChainstateManagerType;
static PyTypeObject BlockTreeEntryType;
static PyTypeObject ChainType_;
static PyTypeObject BlockSpentOutputsType;
static PyTypeObject TransactionSpentOutputsType;
static PyTypeObject CoinType;

static PyObject* KernelError;

/* ------------------------------------------------------------------ */
/* Helpers                                                            */
/* ------------------------------------------------------------------ */

#define CHECK_PTR(obj)                                                        \
    do {                                                                      \
        if ((obj)->ptr == NULL) {                                             \
            PyErr_SetString(PyExc_ValueError, "operation on closed object");  \
            return NULL;                                                      \
        }                                                                     \
    } while (0)

/* Views into the chainstate manager (Chain, BlockTreeEntry) dangle if the
 * manager was explicitly close()d. Owner may also be NULL for entries
 * delivered through callbacks, which we cannot check. Returns 0 and sets an
 * exception if the owner was closed. */
static int
view_owner_alive(PyObject* owner)
{
    if (owner != NULL && PyObject_TypeCheck(owner, &ChainstateManagerType) &&
        ((ChainstateManagerObject*)owner)->ptr == NULL) {
        PyErr_SetString(PyExc_ValueError,
                        "the chainstate manager this object belongs to is closed");
        return 0;
    }
    return 1;
}

#define CHECK_VIEW(self)                       \
    do {                                       \
        if (!view_owner_alive((self)->owner)) { \
            return NULL;                       \
        }                                      \
    } while (0)

/* Byte accumulation buffer for the btck_WriteBytes serialization callback. */
typedef struct {
    char* buf;
    size_t len;
    size_t cap;
    int oom;
} WriteBuf;

static int
writebuf_cb(const void* bytes, size_t size, void* user_data)
{
    WriteBuf* wb = (WriteBuf*)user_data;
    if (wb->len + size > wb->cap) {
        size_t new_cap = wb->cap ? wb->cap : 256;
        while (new_cap < wb->len + size) {
            new_cap *= 2;
        }
        char* new_buf = realloc(wb->buf, new_cap);
        if (new_buf == NULL) {
            wb->oom = 1;
            return -1;
        }
        wb->buf = new_buf;
        wb->cap = new_cap;
    }
    memcpy(wb->buf + wb->len, bytes, size);
    wb->len += size;
    return 0;
}

/* Serialize via a btck *_to_bytes function into a Python bytes object. */
typedef int (*to_bytes_fn)(const void* obj, btck_WriteBytes writer, void* user_data);

static PyObject*
serialize_to_pybytes(const void* obj, to_bytes_fn fn)
{
    WriteBuf wb = {0};
    int rc = fn(obj, writebuf_cb, &wb);
    if (rc != 0 || wb.oom) {
        free(wb.buf);
        if (wb.oom) {
            return PyErr_NoMemory();
        }
        PyErr_SetString(KernelError, "serialization failed");
        return NULL;
    }
    PyObject* result = PyBytes_FromStringAndSize(wb.buf, (Py_ssize_t)wb.len);
    free(wb.buf);
    return result;
}

/* Hex representation helpers: bitcoin hashes are displayed byte-reversed. */
static PyObject*
hash_repr(const char* type_name, const unsigned char data[32])
{
    char hex[65];
    static const char* digits = "0123456789abcdef";
    for (int i = 0; i < 32; i++) {
        unsigned char b = data[31 - i];
        hex[i * 2] = digits[b >> 4];
        hex[i * 2 + 1] = digits[b & 0x0f];
    }
    hex[64] = '\0';
    return PyUnicode_FromFormat("<%s %s>", type_name, hex);
}

static PyObject*
hash_hex_str(const unsigned char data[32])
{
    char hex[65];
    static const char* digits = "0123456789abcdef";
    for (int i = 0; i < 32; i++) {
        unsigned char b = data[31 - i];
        hex[i * 2] = digits[b >> 4];
        hex[i * 2 + 1] = digits[b & 0x0f];
    }
    hex[64] = '\0';
    return PyUnicode_FromStringAndSize(hex, 64);
}

/* Wrapper construction helpers.  Each steals ownership of `ptr`. */

static PyObject*
Transaction_wrap(btck_Transaction* ptr)
{
    TransactionObject* self = PyObject_New(TransactionObject, &TransactionType);
    if (self == NULL) {
        btck_transaction_destroy(ptr);
        return NULL;
    }
    self->ptr = ptr;
    return (PyObject*)self;
}

static PyObject*
ScriptPubkey_wrap(btck_ScriptPubkey* ptr)
{
    ScriptPubkeyObject* self = PyObject_New(ScriptPubkeyObject, &ScriptPubkeyType);
    if (self == NULL) {
        btck_script_pubkey_destroy(ptr);
        return NULL;
    }
    self->ptr = ptr;
    return (PyObject*)self;
}

static PyObject*
TransactionOutput_wrap(btck_TransactionOutput* ptr)
{
    TransactionOutputObject* self = PyObject_New(TransactionOutputObject, &TransactionOutputType);
    if (self == NULL) {
        btck_transaction_output_destroy(ptr);
        return NULL;
    }
    self->ptr = ptr;
    return (PyObject*)self;
}

static PyObject*
TransactionInput_wrap(btck_TransactionInput* ptr)
{
    TransactionInputObject* self = PyObject_New(TransactionInputObject, &TransactionInputType);
    if (self == NULL) {
        btck_transaction_input_destroy(ptr);
        return NULL;
    }
    self->ptr = ptr;
    return (PyObject*)self;
}

static PyObject*
TransactionOutPoint_wrap(btck_TransactionOutPoint* ptr)
{
    TransactionOutPointObject* self =
        PyObject_New(TransactionOutPointObject, &TransactionOutPointType);
    if (self == NULL) {
        btck_transaction_out_point_destroy(ptr);
        return NULL;
    }
    self->ptr = ptr;
    return (PyObject*)self;
}

static PyObject*
Txid_wrap(btck_Txid* ptr)
{
    TxidObject* self = PyObject_New(TxidObject, &TxidType);
    if (self == NULL) {
        btck_txid_destroy(ptr);
        return NULL;
    }
    self->ptr = ptr;
    return (PyObject*)self;
}

static PyObject*
BlockHash_wrap(btck_BlockHash* ptr)
{
    BlockHashObject* self = PyObject_New(BlockHashObject, &BlockHashType);
    if (self == NULL) {
        btck_block_hash_destroy(ptr);
        return NULL;
    }
    self->ptr = ptr;
    return (PyObject*)self;
}

static PyObject*
BlockHeader_wrap(btck_BlockHeader* ptr)
{
    BlockHeaderObject* self = PyObject_New(BlockHeaderObject, &BlockHeaderType);
    if (self == NULL) {
        btck_block_header_destroy(ptr);
        return NULL;
    }
    self->ptr = ptr;
    return (PyObject*)self;
}

static PyObject*
Block_wrap(btck_Block* ptr)
{
    BlockObject* self = PyObject_New(BlockObject, &BlockType);
    if (self == NULL) {
        btck_block_destroy(ptr);
        return NULL;
    }
    self->ptr = ptr;
    return (PyObject*)self;
}

static PyObject*
BlockValidationState_wrap(btck_BlockValidationState* ptr)
{
    BlockValidationStateObject* self =
        PyObject_New(BlockValidationStateObject, &BlockValidationStateType);
    if (self == NULL) {
        btck_block_validation_state_destroy(ptr);
        return NULL;
    }
    self->ptr = ptr;
    return (PyObject*)self;
}

static PyObject*
Coin_wrap(btck_Coin* ptr)
{
    CoinObject* self = PyObject_New(CoinObject, &CoinType);
    if (self == NULL) {
        btck_coin_destroy(ptr);
        return NULL;
    }
    self->ptr = ptr;
    return (PyObject*)self;
}

static PyObject*
TransactionSpentOutputs_wrap(btck_TransactionSpentOutputs* ptr)
{
    TransactionSpentOutputsObject* self =
        PyObject_New(TransactionSpentOutputsObject, &TransactionSpentOutputsType);
    if (self == NULL) {
        btck_transaction_spent_outputs_destroy(ptr);
        return NULL;
    }
    self->ptr = ptr;
    return (PyObject*)self;
}

/* owner may be NULL (e.g. entries delivered through callbacks). */
static PyObject*
BlockTreeEntry_wrap(const btck_BlockTreeEntry* ptr, PyObject* owner)
{
    BlockTreeEntryObject* self = PyObject_New(BlockTreeEntryObject, &BlockTreeEntryType);
    if (self == NULL) {
        return NULL;
    }
    self->ptr = ptr;
    self->owner = owner;
    Py_XINCREF(owner);
    return (PyObject*)self;
}

/* ------------------------------------------------------------------ */
/* Callback trampolines                                               */
/* ------------------------------------------------------------------ */

/* Generic destroy callback for PyObject user data. May be invoked from any
 * thread, including during interpreter-held calls, so take the GIL. */
static void
pyobject_destroy_cb(void* user_data)
{
    if (user_data == NULL) {
        return;
    }
    PyGILState_STATE gil = PyGILState_Ensure();
    Py_DECREF((PyObject*)user_data);
    PyGILState_Release(gil);
}

/* Call handler.<name>(*args) if such an attribute exists; unraisable
 * exceptions are reported and swallowed (we cannot propagate through C). */
static void
call_handler_method(PyObject* handler, const char* name, PyObject* args)
{
    PyObject* method = PyObject_GetAttrString(handler, name);
    if (method == NULL) {
        PyErr_Clear();
        return;
    }
    PyObject* result = PyObject_CallObject(method, args);
    if (result == NULL) {
        PyErr_WriteUnraisable(method);
    }
    Py_XDECREF(result);
    Py_DECREF(method);
}

/* Logging */
static void
log_cb(void* user_data, const char* message, size_t message_len)
{
    PyGILState_STATE gil = PyGILState_Ensure();
    PyObject* callback = (PyObject*)user_data;
    PyObject* msg = PyUnicode_DecodeUTF8(message, (Py_ssize_t)message_len, "replace");
    if (msg != NULL) {
        PyObject* result = PyObject_CallFunctionObjArgs(callback, msg, NULL);
        if (result == NULL) {
            PyErr_WriteUnraisable(callback);
        }
        Py_XDECREF(result);
        Py_DECREF(msg);
    } else {
        PyErr_WriteUnraisable(callback);
    }
    PyGILState_Release(gil);
}

/* Notifications */
static void
notify_block_tip_cb(void* user_data, btck_SynchronizationState state,
                    const btck_BlockTreeEntry* entry, double verification_progress)
{
    PyGILState_STATE gil = PyGILState_Ensure();
    PyObject* entry_obj = BlockTreeEntry_wrap(entry, NULL);
    if (entry_obj != NULL) {
        PyObject* args = Py_BuildValue("(iOd)", (int)state, entry_obj, verification_progress);
        if (args != NULL) {
            call_handler_method((PyObject*)user_data, "block_tip", args);
            Py_DECREF(args);
        } else {
            PyErr_WriteUnraisable((PyObject*)user_data);
        }
        Py_DECREF(entry_obj);
    } else {
        PyErr_WriteUnraisable((PyObject*)user_data);
    }
    PyGILState_Release(gil);
}

static void
notify_header_tip_cb(void* user_data, btck_SynchronizationState state,
                     int64_t height, int64_t timestamp, int presync)
{
    PyGILState_STATE gil = PyGILState_Ensure();
    PyObject* args = Py_BuildValue("(iLLO)", (int)state, (long long)height,
                                   (long long)timestamp, presync ? Py_True : Py_False);
    if (args != NULL) {
        call_handler_method((PyObject*)user_data, "header_tip", args);
        Py_DECREF(args);
    } else {
        PyErr_WriteUnraisable((PyObject*)user_data);
    }
    PyGILState_Release(gil);
}

static void
notify_progress_cb(void* user_data, const char* title, size_t title_len,
                   int progress_percent, int resume_possible)
{
    PyGILState_STATE gil = PyGILState_Ensure();
    PyObject* args = Py_BuildValue("(s#iO)", title, (Py_ssize_t)title_len,
                                   progress_percent, resume_possible ? Py_True : Py_False);
    if (args != NULL) {
        call_handler_method((PyObject*)user_data, "progress", args);
        Py_DECREF(args);
    } else {
        PyErr_WriteUnraisable((PyObject*)user_data);
    }
    PyGILState_Release(gil);
}

static void
notify_warning_set_cb(void* user_data, btck_Warning warning,
                      const char* message, size_t message_len)
{
    PyGILState_STATE gil = PyGILState_Ensure();
    PyObject* args = Py_BuildValue("(is#)", (int)warning, message, (Py_ssize_t)message_len);
    if (args != NULL) {
        call_handler_method((PyObject*)user_data, "warning_set", args);
        Py_DECREF(args);
    } else {
        PyErr_WriteUnraisable((PyObject*)user_data);
    }
    PyGILState_Release(gil);
}

static void
notify_warning_unset_cb(void* user_data, btck_Warning warning)
{
    PyGILState_STATE gil = PyGILState_Ensure();
    PyObject* args = Py_BuildValue("(i)", (int)warning);
    if (args != NULL) {
        call_handler_method((PyObject*)user_data, "warning_unset", args);
        Py_DECREF(args);
    } else {
        PyErr_WriteUnraisable((PyObject*)user_data);
    }
    PyGILState_Release(gil);
}

static void
notify_flush_error_cb(void* user_data, const char* message, size_t message_len)
{
    PyGILState_STATE gil = PyGILState_Ensure();
    PyObject* args = Py_BuildValue("(s#)", message, (Py_ssize_t)message_len);
    if (args != NULL) {
        call_handler_method((PyObject*)user_data, "flush_error", args);
        Py_DECREF(args);
    } else {
        PyErr_WriteUnraisable((PyObject*)user_data);
    }
    PyGILState_Release(gil);
}

static void
notify_fatal_error_cb(void* user_data, const char* message, size_t message_len)
{
    PyGILState_STATE gil = PyGILState_Ensure();
    PyObject* args = Py_BuildValue("(s#)", message, (Py_ssize_t)message_len);
    if (args != NULL) {
        call_handler_method((PyObject*)user_data, "fatal_error", args);
        Py_DECREF(args);
    } else {
        PyErr_WriteUnraisable((PyObject*)user_data);
    }
    PyGILState_Release(gil);
}

/* Validation interface. Blocks are reference counted, so copies are cheap
 * and give the Python wrapper full ownership. */
static void
vi_block_checked_cb(void* user_data, btck_Block* block, const btck_BlockValidationState* state)
{
    PyGILState_STATE gil = PyGILState_Ensure();
    PyObject* block_obj = Block_wrap(btck_block_copy(block));
    PyObject* state_obj =
        block_obj ? BlockValidationState_wrap(btck_block_validation_state_copy(state)) : NULL;
    if (block_obj != NULL && state_obj != NULL) {
        PyObject* args = PyTuple_Pack(2, block_obj, state_obj);
        if (args != NULL) {
            call_handler_method((PyObject*)user_data, "block_checked", args);
            Py_DECREF(args);
        } else {
            PyErr_WriteUnraisable((PyObject*)user_data);
        }
    } else {
        PyErr_WriteUnraisable((PyObject*)user_data);
    }
    Py_XDECREF(block_obj);
    Py_XDECREF(state_obj);
    PyGILState_Release(gil);
}

static void
vi_block_event_cb(void* user_data, const char* method_name,
                  btck_Block* block, const btck_BlockTreeEntry* entry)
{
    PyGILState_STATE gil = PyGILState_Ensure();
    PyObject* block_obj = Block_wrap(btck_block_copy(block));
    PyObject* entry_obj = block_obj ? BlockTreeEntry_wrap(entry, NULL) : NULL;
    if (block_obj != NULL && entry_obj != NULL) {
        PyObject* args = PyTuple_Pack(2, block_obj, entry_obj);
        if (args != NULL) {
            call_handler_method((PyObject*)user_data, method_name, args);
            Py_DECREF(args);
        } else {
            PyErr_WriteUnraisable((PyObject*)user_data);
        }
    } else {
        PyErr_WriteUnraisable((PyObject*)user_data);
    }
    Py_XDECREF(block_obj);
    Py_XDECREF(entry_obj);
    PyGILState_Release(gil);
}

static void
vi_pow_valid_block_cb(void* user_data, btck_Block* block, const btck_BlockTreeEntry* entry)
{
    vi_block_event_cb(user_data, "pow_valid_block", block, entry);
}

static void
vi_block_connected_cb(void* user_data, btck_Block* block, const btck_BlockTreeEntry* entry)
{
    vi_block_event_cb(user_data, "block_connected", block, entry);
}

static void
vi_block_disconnected_cb(void* user_data, btck_Block* block, const btck_BlockTreeEntry* entry)
{
    vi_block_event_cb(user_data, "block_disconnected", block, entry);
}

/* ------------------------------------------------------------------ */
/* Txid                                                               */
/* ------------------------------------------------------------------ */

static void
Txid_dealloc(TxidObject* self)
{
    btck_txid_destroy(self->ptr);
    Py_TYPE(self)->tp_free((PyObject*)self);
}

static PyObject*
Txid_to_bytes(TxidObject* self, PyObject* Py_UNUSED(ignored))
{
    unsigned char out[32];
    btck_txid_to_bytes(self->ptr, out);
    return PyBytes_FromStringAndSize((const char*)out, 32);
}

static PyObject*
Txid_hex(TxidObject* self, PyObject* Py_UNUSED(ignored))
{
    unsigned char out[32];
    btck_txid_to_bytes(self->ptr, out);
    return hash_hex_str(out);
}

static PyObject*
Txid_richcompare(PyObject* a, PyObject* b, int op)
{
    if ((op != Py_EQ && op != Py_NE) ||
        !PyObject_TypeCheck(a, &TxidType) || !PyObject_TypeCheck(b, &TxidType)) {
        Py_RETURN_NOTIMPLEMENTED;
    }
    int eq = btck_txid_equals(((TxidObject*)a)->ptr, ((TxidObject*)b)->ptr);
    if (op == Py_NE) {
        eq = !eq;
    }
    return PyBool_FromLong(eq);
}

static Py_hash_t
Txid_hash(TxidObject* self)
{
    unsigned char out[32];
    btck_txid_to_bytes(self->ptr, out);
    Py_hash_t h;
    memcpy(&h, out, sizeof(h));
    if (h == -1) {
        h = -2;
    }
    return h;
}

static PyObject*
Txid_repr(TxidObject* self)
{
    unsigned char out[32];
    btck_txid_to_bytes(self->ptr, out);
    return hash_repr("Txid", out);
}

static PyMethodDef Txid_methods[] = {
    {"to_bytes", (PyCFunction)Txid_to_bytes, METH_NOARGS,
     "Serialize the txid to 32 bytes (internal byte order)."},
    {"hex", (PyCFunction)Txid_hex, METH_NOARGS,
     "Return the txid as display hex (byte-reversed, as used by explorers)."},
    {NULL, NULL, 0, NULL},
};

static PyTypeObject TxidType = {
    .ob_base = PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "pybitcoinkernel._bitcoinkernel.Txid",
    .tp_basicsize = sizeof(TxidObject),
    .tp_dealloc = (destructor)Txid_dealloc,
    .tp_flags = Py_TPFLAGS_DEFAULT,
    .tp_doc = PyDoc_STR("A transaction identifier."),
    .tp_methods = Txid_methods,
    .tp_richcompare = Txid_richcompare,
    .tp_hash = (hashfunc)Txid_hash,
    .tp_repr = (reprfunc)Txid_repr,
};

/* ------------------------------------------------------------------ */
/* BlockHash                                                          */
/* ------------------------------------------------------------------ */

static void
BlockHash_dealloc(BlockHashObject* self)
{
    btck_block_hash_destroy(self->ptr);
    Py_TYPE(self)->tp_free((PyObject*)self);
}

static PyObject*
BlockHash_new(PyTypeObject* type, PyObject* args, PyObject* kwds)
{
    Py_buffer data;
    static char* kwlist[] = {"data", NULL};
    if (!PyArg_ParseTupleAndKeywords(args, kwds, "y*", kwlist, &data)) {
        return NULL;
    }
    if (data.len != 32) {
        PyBuffer_Release(&data);
        PyErr_SetString(PyExc_ValueError, "block hash must be exactly 32 bytes");
        return NULL;
    }
    btck_BlockHash* ptr = btck_block_hash_create((const unsigned char*)data.buf);
    PyBuffer_Release(&data);
    if (ptr == NULL) {
        PyErr_SetString(KernelError, "failed to create block hash");
        return NULL;
    }
    BlockHashObject* self = (BlockHashObject*)type->tp_alloc(type, 0);
    if (self == NULL) {
        btck_block_hash_destroy(ptr);
        return NULL;
    }
    self->ptr = ptr;
    return (PyObject*)self;
}

static PyObject*
BlockHash_to_bytes(BlockHashObject* self, PyObject* Py_UNUSED(ignored))
{
    unsigned char out[32];
    btck_block_hash_to_bytes(self->ptr, out);
    return PyBytes_FromStringAndSize((const char*)out, 32);
}

static PyObject*
BlockHash_hex(BlockHashObject* self, PyObject* Py_UNUSED(ignored))
{
    unsigned char out[32];
    btck_block_hash_to_bytes(self->ptr, out);
    return hash_hex_str(out);
}

static PyObject*
BlockHash_richcompare(PyObject* a, PyObject* b, int op)
{
    if ((op != Py_EQ && op != Py_NE) ||
        !PyObject_TypeCheck(a, &BlockHashType) || !PyObject_TypeCheck(b, &BlockHashType)) {
        Py_RETURN_NOTIMPLEMENTED;
    }
    int eq = btck_block_hash_equals(((BlockHashObject*)a)->ptr, ((BlockHashObject*)b)->ptr);
    if (op == Py_NE) {
        eq = !eq;
    }
    return PyBool_FromLong(eq);
}

static Py_hash_t
BlockHash_hash(BlockHashObject* self)
{
    unsigned char out[32];
    btck_block_hash_to_bytes(self->ptr, out);
    Py_hash_t h;
    memcpy(&h, out, sizeof(h));
    if (h == -1) {
        h = -2;
    }
    return h;
}

static PyObject*
BlockHash_repr(BlockHashObject* self)
{
    unsigned char out[32];
    btck_block_hash_to_bytes(self->ptr, out);
    return hash_repr("BlockHash", out);
}

static PyMethodDef BlockHash_methods[] = {
    {"to_bytes", (PyCFunction)BlockHash_to_bytes, METH_NOARGS,
     "Serialize the block hash to 32 bytes (internal byte order)."},
    {"hex", (PyCFunction)BlockHash_hex, METH_NOARGS,
     "Return the block hash as display hex (byte-reversed, as used by explorers)."},
    {NULL, NULL, 0, NULL},
};

static PyTypeObject BlockHashType = {
    .ob_base = PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "pybitcoinkernel._bitcoinkernel.BlockHash",
    .tp_basicsize = sizeof(BlockHashObject),
    .tp_dealloc = (destructor)BlockHash_dealloc,
    .tp_flags = Py_TPFLAGS_DEFAULT,
    .tp_doc = PyDoc_STR("BlockHash(data: bytes) - a 32-byte block identifier."),
    .tp_new = BlockHash_new,
    .tp_methods = BlockHash_methods,
    .tp_richcompare = BlockHash_richcompare,
    .tp_hash = (hashfunc)BlockHash_hash,
    .tp_repr = (reprfunc)BlockHash_repr,
};

/* ------------------------------------------------------------------ */
/* ScriptPubkey                                                       */
/* ------------------------------------------------------------------ */

static void
ScriptPubkey_dealloc(ScriptPubkeyObject* self)
{
    btck_script_pubkey_destroy(self->ptr);
    Py_TYPE(self)->tp_free((PyObject*)self);
}

static PyObject*
ScriptPubkey_new(PyTypeObject* type, PyObject* args, PyObject* kwds)
{
    Py_buffer data;
    static char* kwlist[] = {"data", NULL};
    if (!PyArg_ParseTupleAndKeywords(args, kwds, "y*", kwlist, &data)) {
        return NULL;
    }
    btck_ScriptPubkey* ptr = btck_script_pubkey_create(data.buf, (size_t)data.len);
    PyBuffer_Release(&data);
    if (ptr == NULL) {
        PyErr_SetString(KernelError, "failed to create script pubkey");
        return NULL;
    }
    ScriptPubkeyObject* self = (ScriptPubkeyObject*)type->tp_alloc(type, 0);
    if (self == NULL) {
        btck_script_pubkey_destroy(ptr);
        return NULL;
    }
    self->ptr = ptr;
    return (PyObject*)self;
}

static PyObject*
ScriptPubkey_to_bytes(ScriptPubkeyObject* self, PyObject* Py_UNUSED(ignored))
{
    return serialize_to_pybytes(self->ptr, (to_bytes_fn)btck_script_pubkey_to_bytes);
}

static PyObject*
ScriptPubkey_verify(ScriptPubkeyObject* self, PyObject* args, PyObject* kwds)
{
    long long amount = 0;
    PyObject* tx_obj;
    unsigned int input_index;
    unsigned long flags = btck_ScriptVerificationFlags_ALL;
    PyObject* precomputed_obj = Py_None;
    static char* kwlist[] = {"amount", "tx_to", "input_index", "flags", "precomputed_transaction_data", NULL};
    if (!PyArg_ParseTupleAndKeywords(args, kwds, "LO!I|kO", kwlist,
                                     &amount, &TransactionType, &tx_obj, &input_index,
                                     &flags, &precomputed_obj)) {
        return NULL;
    }

    const btck_PrecomputedTransactionData* precomputed = NULL;
    if (precomputed_obj != Py_None) {
        if (!PyObject_TypeCheck(precomputed_obj, &PrecomputedTransactionDataType)) {
            PyErr_SetString(PyExc_TypeError,
                            "precomputed_transaction_data must be PrecomputedTransactionData or None");
            return NULL;
        }
        precomputed = ((PrecomputedTransactionDataObject*)precomputed_obj)->ptr;
    }

    /* The kernel asserts (aborts) on these, so reject them here. */
    if ((flags & ~(unsigned long)btck_ScriptVerificationFlags_ALL) != 0) {
        PyErr_SetString(PyExc_ValueError, "unknown script verification flags set");
        return NULL;
    }
    size_t n_inputs = btck_transaction_count_inputs(((TransactionObject*)tx_obj)->ptr);
    if ((size_t)input_index >= n_inputs) {
        PyErr_SetString(PyExc_IndexError, "input_index out of range for tx_to");
        return NULL;
    }

    btck_ScriptVerifyStatus status = btck_ScriptVerifyStatus_OK;
    int valid = btck_script_pubkey_verify(
        self->ptr, (int64_t)amount, ((TransactionObject*)tx_obj)->ptr,
        precomputed, input_index, (btck_ScriptVerificationFlags)flags, &status);
    if (status == btck_ScriptVerifyStatus_ERROR_INVALID_FLAGS_COMBINATION) {
        PyErr_SetString(PyExc_ValueError, "invalid combination of script verification flags");
        return NULL;
    }
    if (status == btck_ScriptVerifyStatus_ERROR_SPENT_OUTPUTS_REQUIRED) {
        PyErr_SetString(PyExc_ValueError,
                        "taproot verification requires precomputed transaction data "
                        "with spent outputs");
        return NULL;
    }
    return PyBool_FromLong(valid);
}

static PyMethodDef ScriptPubkey_methods[] = {
    {"to_bytes", (PyCFunction)ScriptPubkey_to_bytes, METH_NOARGS,
     "Serialize the script pubkey to bytes."},
    {"verify", (PyCFunction)(void (*)(void))ScriptPubkey_verify, METH_VARARGS | METH_KEYWORDS,
     "verify(amount, tx_to, input_index, flags=SCRIPT_FLAGS_VERIFY_ALL, "
     "precomputed_transaction_data=None) -> bool\n\n"
     "Verify that input_index of tx_to validly spends this script pubkey."},
    {NULL, NULL, 0, NULL},
};

static PyTypeObject ScriptPubkeyType = {
    .ob_base = PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "pybitcoinkernel._bitcoinkernel.ScriptPubkey",
    .tp_basicsize = sizeof(ScriptPubkeyObject),
    .tp_dealloc = (destructor)ScriptPubkey_dealloc,
    .tp_flags = Py_TPFLAGS_DEFAULT,
    .tp_doc = PyDoc_STR("ScriptPubkey(data: bytes) - a serialized output script."),
    .tp_new = ScriptPubkey_new,
    .tp_methods = ScriptPubkey_methods,
};

/* ------------------------------------------------------------------ */
/* TransactionOutput                                                  */
/* ------------------------------------------------------------------ */

static void
TransactionOutput_dealloc(TransactionOutputObject* self)
{
    btck_transaction_output_destroy(self->ptr);
    Py_TYPE(self)->tp_free((PyObject*)self);
}

static PyObject*
TransactionOutput_new(PyTypeObject* type, PyObject* args, PyObject* kwds)
{
    PyObject* script_obj;
    long long amount;
    static char* kwlist[] = {"script_pubkey", "amount", NULL};
    if (!PyArg_ParseTupleAndKeywords(args, kwds, "O!L", kwlist,
                                     &ScriptPubkeyType, &script_obj, &amount)) {
        return NULL;
    }
    btck_TransactionOutput* ptr = btck_transaction_output_create(
        ((ScriptPubkeyObject*)script_obj)->ptr, (int64_t)amount);
    if (ptr == NULL) {
        PyErr_SetString(KernelError, "failed to create transaction output");
        return NULL;
    }
    TransactionOutputObject* self = (TransactionOutputObject*)type->tp_alloc(type, 0);
    if (self == NULL) {
        btck_transaction_output_destroy(ptr);
        return NULL;
    }
    self->ptr = ptr;
    return (PyObject*)self;
}

static PyObject*
TransactionOutput_get_amount(TransactionOutputObject* self, void* closure)
{
    return PyLong_FromLongLong((long long)btck_transaction_output_get_amount(self->ptr));
}

static PyObject*
TransactionOutput_get_script_pubkey(TransactionOutputObject* self, void* closure)
{
    const btck_ScriptPubkey* view = btck_transaction_output_get_script_pubkey(self->ptr);
    return ScriptPubkey_wrap(btck_script_pubkey_copy(view));
}

static PyGetSetDef TransactionOutput_getset[] = {
    {"amount", (getter)TransactionOutput_get_amount, NULL,
     "The amount of the output in satoshis.", NULL},
    {"script_pubkey", (getter)TransactionOutput_get_script_pubkey, NULL,
     "The script pubkey of the output.", NULL},
    {NULL, NULL, NULL, NULL, NULL},
};

static PyTypeObject TransactionOutputType = {
    .ob_base = PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "pybitcoinkernel._bitcoinkernel.TransactionOutput",
    .tp_basicsize = sizeof(TransactionOutputObject),
    .tp_dealloc = (destructor)TransactionOutput_dealloc,
    .tp_flags = Py_TPFLAGS_DEFAULT,
    .tp_doc = PyDoc_STR("TransactionOutput(script_pubkey: ScriptPubkey, amount: int)"),
    .tp_new = TransactionOutput_new,
    .tp_getset = TransactionOutput_getset,
};

/* ------------------------------------------------------------------ */
/* TransactionOutPoint                                                */
/* ------------------------------------------------------------------ */

static void
TransactionOutPoint_dealloc(TransactionOutPointObject* self)
{
    btck_transaction_out_point_destroy(self->ptr);
    Py_TYPE(self)->tp_free((PyObject*)self);
}

static PyObject*
TransactionOutPoint_get_index(TransactionOutPointObject* self, void* closure)
{
    return PyLong_FromUnsignedLong(btck_transaction_out_point_get_index(self->ptr));
}

static PyObject*
TransactionOutPoint_get_txid(TransactionOutPointObject* self, void* closure)
{
    const btck_Txid* view = btck_transaction_out_point_get_txid(self->ptr);
    return Txid_wrap(btck_txid_copy(view));
}

static PyGetSetDef TransactionOutPoint_getset[] = {
    {"index", (getter)TransactionOutPoint_get_index, NULL,
     "The output index this out point refers to.", NULL},
    {"txid", (getter)TransactionOutPoint_get_txid, NULL,
     "The txid this out point refers to.", NULL},
    {NULL, NULL, NULL, NULL, NULL},
};

static PyTypeObject TransactionOutPointType = {
    .ob_base = PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "pybitcoinkernel._bitcoinkernel.TransactionOutPoint",
    .tp_basicsize = sizeof(TransactionOutPointObject),
    .tp_dealloc = (destructor)TransactionOutPoint_dealloc,
    .tp_flags = Py_TPFLAGS_DEFAULT,
    .tp_doc = PyDoc_STR("A reference to a transaction output (txid, index)."),
    .tp_getset = TransactionOutPoint_getset,
};

/* ------------------------------------------------------------------ */
/* TransactionInput                                                   */
/* ------------------------------------------------------------------ */

static void
TransactionInput_dealloc(TransactionInputObject* self)
{
    btck_transaction_input_destroy(self->ptr);
    Py_TYPE(self)->tp_free((PyObject*)self);
}

static PyObject*
TransactionInput_get_out_point(TransactionInputObject* self, void* closure)
{
    const btck_TransactionOutPoint* view = btck_transaction_input_get_out_point(self->ptr);
    return TransactionOutPoint_wrap(btck_transaction_out_point_copy(view));
}

static PyGetSetDef TransactionInput_getset[] = {
    {"out_point", (getter)TransactionInput_get_out_point, NULL,
     "The out point spent by this input.", NULL},
    {NULL, NULL, NULL, NULL, NULL},
};

static PyTypeObject TransactionInputType = {
    .ob_base = PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "pybitcoinkernel._bitcoinkernel.TransactionInput",
    .tp_basicsize = sizeof(TransactionInputObject),
    .tp_dealloc = (destructor)TransactionInput_dealloc,
    .tp_flags = Py_TPFLAGS_DEFAULT,
    .tp_doc = PyDoc_STR("A transaction input."),
    .tp_getset = TransactionInput_getset,
};

/* ------------------------------------------------------------------ */
/* Transaction                                                        */
/* ------------------------------------------------------------------ */

static void
Transaction_dealloc(TransactionObject* self)
{
    btck_transaction_destroy(self->ptr);
    Py_TYPE(self)->tp_free((PyObject*)self);
}

static PyObject*
Transaction_new(PyTypeObject* type, PyObject* args, PyObject* kwds)
{
    Py_buffer data;
    static char* kwlist[] = {"data", NULL};
    if (!PyArg_ParseTupleAndKeywords(args, kwds, "y*", kwlist, &data)) {
        return NULL;
    }
    btck_Transaction* ptr = btck_transaction_create(data.buf, (size_t)data.len);
    PyBuffer_Release(&data);
    if (ptr == NULL) {
        PyErr_SetString(KernelError, "failed to deserialize transaction");
        return NULL;
    }
    TransactionObject* self = (TransactionObject*)type->tp_alloc(type, 0);
    if (self == NULL) {
        btck_transaction_destroy(ptr);
        return NULL;
    }
    self->ptr = ptr;
    return (PyObject*)self;
}

static PyObject*
Transaction_to_bytes(TransactionObject* self, PyObject* Py_UNUSED(ignored))
{
    return serialize_to_pybytes(self->ptr, (to_bytes_fn)btck_transaction_to_bytes);
}

static PyObject*
Transaction_get_txid(TransactionObject* self, void* closure)
{
    const btck_Txid* view = btck_transaction_get_txid(self->ptr);
    return Txid_wrap(btck_txid_copy(view));
}

static PyObject*
Transaction_get_n_inputs(TransactionObject* self, void* closure)
{
    return PyLong_FromSize_t(btck_transaction_count_inputs(self->ptr));
}

static PyObject*
Transaction_get_n_outputs(TransactionObject* self, void* closure)
{
    return PyLong_FromSize_t(btck_transaction_count_outputs(self->ptr));
}

static PyObject*
Transaction_get_input(TransactionObject* self, PyObject* arg)
{
    Py_ssize_t index = PyNumber_AsSsize_t(arg, PyExc_IndexError);
    if (index == -1 && PyErr_Occurred()) {
        return NULL;
    }
    Py_ssize_t count = (Py_ssize_t)btck_transaction_count_inputs(self->ptr);
    if (index < 0) {
        index += count;
    }
    if (index < 0 || index >= count) {
        PyErr_SetString(PyExc_IndexError, "input index out of range");
        return NULL;
    }
    const btck_TransactionInput* view = btck_transaction_get_input_at(self->ptr, (size_t)index);
    return TransactionInput_wrap(btck_transaction_input_copy(view));
}

static PyObject*
Transaction_get_output(TransactionObject* self, PyObject* arg)
{
    Py_ssize_t index = PyNumber_AsSsize_t(arg, PyExc_IndexError);
    if (index == -1 && PyErr_Occurred()) {
        return NULL;
    }
    Py_ssize_t count = (Py_ssize_t)btck_transaction_count_outputs(self->ptr);
    if (index < 0) {
        index += count;
    }
    if (index < 0 || index >= count) {
        PyErr_SetString(PyExc_IndexError, "output index out of range");
        return NULL;
    }
    const btck_TransactionOutput* view = btck_transaction_get_output_at(self->ptr, (size_t)index);
    return TransactionOutput_wrap(btck_transaction_output_copy(view));
}

static PyObject*
Transaction_get_inputs(TransactionObject* self, void* closure)
{
    size_t count = btck_transaction_count_inputs(self->ptr);
    PyObject* result = PyTuple_New((Py_ssize_t)count);
    if (result == NULL) {
        return NULL;
    }
    for (size_t i = 0; i < count; i++) {
        const btck_TransactionInput* view = btck_transaction_get_input_at(self->ptr, i);
        PyObject* item = TransactionInput_wrap(btck_transaction_input_copy(view));
        if (item == NULL) {
            Py_DECREF(result);
            return NULL;
        }
        PyTuple_SET_ITEM(result, (Py_ssize_t)i, item);
    }
    return result;
}

static PyObject*
Transaction_get_outputs(TransactionObject* self, void* closure)
{
    size_t count = btck_transaction_count_outputs(self->ptr);
    PyObject* result = PyTuple_New((Py_ssize_t)count);
    if (result == NULL) {
        return NULL;
    }
    for (size_t i = 0; i < count; i++) {
        const btck_TransactionOutput* view = btck_transaction_get_output_at(self->ptr, i);
        PyObject* item = TransactionOutput_wrap(btck_transaction_output_copy(view));
        if (item == NULL) {
            Py_DECREF(result);
            return NULL;
        }
        PyTuple_SET_ITEM(result, (Py_ssize_t)i, item);
    }
    return result;
}

static PyMethodDef Transaction_methods[] = {
    {"to_bytes", (PyCFunction)Transaction_to_bytes, METH_NOARGS,
     "Serialize the transaction to consensus (P2P network) format."},
    {"input", (PyCFunction)Transaction_get_input, METH_O,
     "input(index) -> TransactionInput"},
    {"output", (PyCFunction)Transaction_get_output, METH_O,
     "output(index) -> TransactionOutput"},
    {NULL, NULL, 0, NULL},
};

static PyGetSetDef Transaction_getset[] = {
    {"txid", (getter)Transaction_get_txid, NULL, "The transaction's txid.", NULL},
    {"n_inputs", (getter)Transaction_get_n_inputs, NULL, "Number of inputs.", NULL},
    {"n_outputs", (getter)Transaction_get_n_outputs, NULL, "Number of outputs.", NULL},
    {"inputs", (getter)Transaction_get_inputs, NULL, "Tuple of all inputs.", NULL},
    {"outputs", (getter)Transaction_get_outputs, NULL, "Tuple of all outputs.", NULL},
    {NULL, NULL, NULL, NULL, NULL},
};

static PyTypeObject TransactionType = {
    .ob_base = PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "pybitcoinkernel._bitcoinkernel.Transaction",
    .tp_basicsize = sizeof(TransactionObject),
    .tp_dealloc = (destructor)Transaction_dealloc,
    .tp_flags = Py_TPFLAGS_DEFAULT,
    .tp_doc = PyDoc_STR("Transaction(data: bytes) - deserialize a bitcoin transaction."),
    .tp_new = Transaction_new,
    .tp_methods = Transaction_methods,
    .tp_getset = Transaction_getset,
};

/* ------------------------------------------------------------------ */
/* PrecomputedTransactionData                                         */
/* ------------------------------------------------------------------ */

static void
PrecomputedTransactionData_dealloc(PrecomputedTransactionDataObject* self)
{
    btck_precomputed_transaction_data_destroy(self->ptr);
    Py_TYPE(self)->tp_free((PyObject*)self);
}

static PyObject*
PrecomputedTransactionData_new(PyTypeObject* type, PyObject* args, PyObject* kwds)
{
    PyObject* tx_obj;
    PyObject* spent_obj = Py_None;
    static char* kwlist[] = {"tx_to", "spent_outputs", NULL};
    if (!PyArg_ParseTupleAndKeywords(args, kwds, "O!|O", kwlist,
                                     &TransactionType, &tx_obj, &spent_obj)) {
        return NULL;
    }

    const btck_TransactionOutput** spent = NULL;
    size_t spent_len = 0;
    PyObject* seq = NULL;
    if (spent_obj != Py_None) {
        seq = PySequence_Fast(spent_obj, "spent_outputs must be a sequence or None");
        if (seq == NULL) {
            return NULL;
        }
        spent_len = (size_t)PySequence_Fast_GET_SIZE(seq);
        /* The kernel asserts (aborts) when a non-empty spent_outputs array
         * does not match the transaction's input count. */
        size_t n_inputs =
            btck_transaction_count_inputs(((TransactionObject*)tx_obj)->ptr);
        if (spent_len != 0 && spent_len != n_inputs) {
            Py_DECREF(seq);
            PyErr_Format(PyExc_ValueError,
                         "spent_outputs has %zu entries but the transaction has "
                         "%zu inputs; provide one spent output per input",
                         spent_len, n_inputs);
            return NULL;
        }
        if (spent_len > 0) {
            spent = PyMem_Malloc(spent_len * sizeof(*spent));
            if (spent == NULL) {
                Py_DECREF(seq);
                return PyErr_NoMemory();
            }
            for (size_t i = 0; i < spent_len; i++) {
                PyObject* item = PySequence_Fast_GET_ITEM(seq, (Py_ssize_t)i);
                if (!PyObject_TypeCheck(item, &TransactionOutputType)) {
                    PyMem_Free(spent);
                    Py_DECREF(seq);
                    PyErr_SetString(PyExc_TypeError,
                                    "spent_outputs items must be TransactionOutput");
                    return NULL;
                }
                spent[i] = ((TransactionOutputObject*)item)->ptr;
            }
        }
    }

    btck_PrecomputedTransactionData* ptr = btck_precomputed_transaction_data_create(
        ((TransactionObject*)tx_obj)->ptr, spent, spent_len);
    PyMem_Free(spent);
    Py_XDECREF(seq);
    if (ptr == NULL) {
        PyErr_SetString(KernelError, "failed to create precomputed transaction data "
                                     "(spent outputs may not match transaction inputs)");
        return NULL;
    }
    PrecomputedTransactionDataObject* self =
        (PrecomputedTransactionDataObject*)type->tp_alloc(type, 0);
    if (self == NULL) {
        btck_precomputed_transaction_data_destroy(ptr);
        return NULL;
    }
    self->ptr = ptr;
    return (PyObject*)self;
}

static PyTypeObject PrecomputedTransactionDataType = {
    .ob_base = PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "pybitcoinkernel._bitcoinkernel.PrecomputedTransactionData",
    .tp_basicsize = sizeof(PrecomputedTransactionDataObject),
    .tp_dealloc = (destructor)PrecomputedTransactionData_dealloc,
    .tp_flags = Py_TPFLAGS_DEFAULT,
    .tp_doc = PyDoc_STR("PrecomputedTransactionData(tx_to: Transaction, "
                        "spent_outputs: Sequence[TransactionOutput] | None = None)\n\n"
                        "Precomputed hashes for verifying multiple inputs of one\n"
                        "transaction. Required (with spent outputs) for taproot."),
    .tp_new = PrecomputedTransactionData_new,
};

/* ------------------------------------------------------------------ */
/* BlockHeader                                                        */
/* ------------------------------------------------------------------ */

static void
BlockHeader_dealloc(BlockHeaderObject* self)
{
    btck_block_header_destroy(self->ptr);
    Py_TYPE(self)->tp_free((PyObject*)self);
}

static PyObject*
BlockHeader_new(PyTypeObject* type, PyObject* args, PyObject* kwds)
{
    Py_buffer data;
    static char* kwlist[] = {"data", NULL};
    if (!PyArg_ParseTupleAndKeywords(args, kwds, "y*", kwlist, &data)) {
        return NULL;
    }
    /* The kernel asserts (aborts) on any other length. */
    if (data.len != 80) {
        PyBuffer_Release(&data);
        PyErr_SetString(PyExc_ValueError, "block header must be exactly 80 bytes");
        return NULL;
    }
    btck_BlockHeader* ptr = btck_block_header_create(data.buf, (size_t)data.len);
    PyBuffer_Release(&data);
    if (ptr == NULL) {
        PyErr_SetString(KernelError, "failed to deserialize block header");
        return NULL;
    }
    BlockHeaderObject* self = (BlockHeaderObject*)type->tp_alloc(type, 0);
    if (self == NULL) {
        btck_block_header_destroy(ptr);
        return NULL;
    }
    self->ptr = ptr;
    return (PyObject*)self;
}

static PyObject*
BlockHeader_get_hash(BlockHeaderObject* self, void* closure)
{
    return BlockHash_wrap(btck_block_header_get_hash(self->ptr));
}

static PyObject*
BlockHeader_get_prev_hash(BlockHeaderObject* self, void* closure)
{
    const btck_BlockHash* view = btck_block_header_get_prev_hash(self->ptr);
    return BlockHash_wrap(btck_block_hash_copy(view));
}

static PyObject*
BlockHeader_get_timestamp(BlockHeaderObject* self, void* closure)
{
    return PyLong_FromUnsignedLong(btck_block_header_get_timestamp(self->ptr));
}

static PyObject*
BlockHeader_get_bits(BlockHeaderObject* self, void* closure)
{
    return PyLong_FromUnsignedLong(btck_block_header_get_bits(self->ptr));
}

static PyObject*
BlockHeader_get_version(BlockHeaderObject* self, void* closure)
{
    return PyLong_FromLong(btck_block_header_get_version(self->ptr));
}

static PyObject*
BlockHeader_get_nonce(BlockHeaderObject* self, void* closure)
{
    return PyLong_FromUnsignedLong(btck_block_header_get_nonce(self->ptr));
}

static PyGetSetDef BlockHeader_getset[] = {
    {"hash", (getter)BlockHeader_get_hash, NULL, "The header's block hash.", NULL},
    {"prev_hash", (getter)BlockHeader_get_prev_hash, NULL,
     "The previous block's hash.", NULL},
    {"timestamp", (getter)BlockHeader_get_timestamp, NULL,
     "Block timestamp (Unix epoch seconds).", NULL},
    {"bits", (getter)BlockHeader_get_bits, NULL,
     "The nBits compact difficulty target.", NULL},
    {"version", (getter)BlockHeader_get_version, NULL, "Block version.", NULL},
    {"nonce", (getter)BlockHeader_get_nonce, NULL, "Block nonce.", NULL},
    {NULL, NULL, NULL, NULL, NULL},
};

static PyTypeObject BlockHeaderType = {
    .ob_base = PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "pybitcoinkernel._bitcoinkernel.BlockHeader",
    .tp_basicsize = sizeof(BlockHeaderObject),
    .tp_dealloc = (destructor)BlockHeader_dealloc,
    .tp_flags = Py_TPFLAGS_DEFAULT,
    .tp_doc = PyDoc_STR("BlockHeader(data: bytes) - an 80-byte serialized block header."),
    .tp_new = BlockHeader_new,
    .tp_getset = BlockHeader_getset,
};

/* ------------------------------------------------------------------ */
/* Block                                                              */
/* ------------------------------------------------------------------ */

static void
Block_dealloc(BlockObject* self)
{
    btck_block_destroy(self->ptr);
    Py_TYPE(self)->tp_free((PyObject*)self);
}

static PyObject*
Block_new(PyTypeObject* type, PyObject* args, PyObject* kwds)
{
    Py_buffer data;
    static char* kwlist[] = {"data", NULL};
    if (!PyArg_ParseTupleAndKeywords(args, kwds, "y*", kwlist, &data)) {
        return NULL;
    }
    btck_Block* ptr = btck_block_create(data.buf, (size_t)data.len);
    PyBuffer_Release(&data);
    if (ptr == NULL) {
        PyErr_SetString(KernelError, "failed to deserialize block");
        return NULL;
    }
    BlockObject* self = (BlockObject*)type->tp_alloc(type, 0);
    if (self == NULL) {
        btck_block_destroy(ptr);
        return NULL;
    }
    self->ptr = ptr;
    return (PyObject*)self;
}

static PyObject*
Block_to_bytes(BlockObject* self, PyObject* Py_UNUSED(ignored))
{
    return serialize_to_pybytes(self->ptr, (to_bytes_fn)btck_block_to_bytes);
}

static PyObject*
Block_get_hash(BlockObject* self, void* closure)
{
    return BlockHash_wrap(btck_block_get_hash(self->ptr));
}

static PyObject*
Block_get_header(BlockObject* self, void* closure)
{
    return BlockHeader_wrap(btck_block_get_header(self->ptr));
}

static Py_ssize_t
Block_length(BlockObject* self)
{
    return (Py_ssize_t)btck_block_count_transactions(self->ptr);
}

static PyObject*
Block_getitem(BlockObject* self, Py_ssize_t index)
{
    Py_ssize_t count = (Py_ssize_t)btck_block_count_transactions(self->ptr);
    if (index < 0) {
        index += count;
    }
    if (index < 0 || index >= count) {
        PyErr_SetString(PyExc_IndexError, "transaction index out of range");
        return NULL;
    }
    const btck_Transaction* view = btck_block_get_transaction_at(self->ptr, (size_t)index);
    return Transaction_wrap(btck_transaction_copy(view));
}

static PySequenceMethods Block_as_sequence = {
    .sq_length = (lenfunc)Block_length,
    .sq_item = (ssizeargfunc)Block_getitem,
};

static PyMethodDef Block_methods[] = {
    {"to_bytes", (PyCFunction)Block_to_bytes, METH_NOARGS,
     "Serialize the block to consensus (P2P network) format."},
    {NULL, NULL, 0, NULL},
};

static PyGetSetDef Block_getset[] = {
    {"hash", (getter)Block_get_hash, NULL, "The block's hash.", NULL},
    {"header", (getter)Block_get_header, NULL, "The block's header.", NULL},
    {NULL, NULL, NULL, NULL, NULL},
};

static PyTypeObject BlockType = {
    .ob_base = PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "pybitcoinkernel._bitcoinkernel.Block",
    .tp_basicsize = sizeof(BlockObject),
    .tp_dealloc = (destructor)Block_dealloc,
    .tp_flags = Py_TPFLAGS_DEFAULT,
    .tp_doc = PyDoc_STR("Block(data: bytes) - deserialize a bitcoin block.\n\n"
                        "Blocks act as sequences of transactions: len(block),\n"
                        "block[i] and iteration are supported."),
    .tp_new = Block_new,
    .tp_methods = Block_methods,
    .tp_getset = Block_getset,
    .tp_as_sequence = &Block_as_sequence,
};

/* ------------------------------------------------------------------ */
/* BlockValidationState                                               */
/* ------------------------------------------------------------------ */

static void
BlockValidationState_dealloc(BlockValidationStateObject* self)
{
    btck_block_validation_state_destroy(self->ptr);
    Py_TYPE(self)->tp_free((PyObject*)self);
}

static PyObject*
BlockValidationState_get_mode(BlockValidationStateObject* self, void* closure)
{
    return PyLong_FromLong((long)btck_block_validation_state_get_validation_mode(self->ptr));
}

static PyObject*
BlockValidationState_get_result(BlockValidationStateObject* self, void* closure)
{
    return PyLong_FromUnsignedLong(
        (unsigned long)btck_block_validation_state_get_block_validation_result(self->ptr));
}

static PyGetSetDef BlockValidationState_getset[] = {
    {"mode", (getter)BlockValidationState_get_mode, NULL,
     "Validation mode: VALIDATION_MODE_VALID / _INVALID / _INTERNAL_ERROR.", NULL},
    {"result", (getter)BlockValidationState_get_result, NULL,
     "Granular BLOCK_VALIDATION_RESULT_* reason for invalidity.", NULL},
    {NULL, NULL, NULL, NULL, NULL},
};

static PyTypeObject BlockValidationStateType = {
    .ob_base = PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "pybitcoinkernel._bitcoinkernel.BlockValidationState",
    .tp_basicsize = sizeof(BlockValidationStateObject),
    .tp_dealloc = (destructor)BlockValidationState_dealloc,
    .tp_flags = Py_TPFLAGS_DEFAULT,
    .tp_doc = PyDoc_STR("The outcome of validating a block or block header."),
    .tp_getset = BlockValidationState_getset,
};

/* ------------------------------------------------------------------ */
/* Coin                                                               */
/* ------------------------------------------------------------------ */

static void
Coin_dealloc(CoinObject* self)
{
    btck_coin_destroy(self->ptr);
    Py_TYPE(self)->tp_free((PyObject*)self);
}

static PyObject*
Coin_get_confirmation_height(CoinObject* self, void* closure)
{
    return PyLong_FromUnsignedLong(btck_coin_confirmation_height(self->ptr));
}

static PyObject*
Coin_get_is_coinbase(CoinObject* self, void* closure)
{
    return PyBool_FromLong(btck_coin_is_coinbase(self->ptr));
}

static PyObject*
Coin_get_output(CoinObject* self, void* closure)
{
    const btck_TransactionOutput* view = btck_coin_get_output(self->ptr);
    return TransactionOutput_wrap(btck_transaction_output_copy(view));
}

static PyGetSetDef Coin_getset[] = {
    {"confirmation_height", (getter)Coin_get_confirmation_height, NULL,
     "Height of the block that created this coin.", NULL},
    {"is_coinbase", (getter)Coin_get_is_coinbase, NULL,
     "Whether the coin was created by a coinbase transaction.", NULL},
    {"output", (getter)Coin_get_output, NULL,
     "The transaction output of this coin.", NULL},
    {NULL, NULL, NULL, NULL, NULL},
};

static PyTypeObject CoinType = {
    .ob_base = PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "pybitcoinkernel._bitcoinkernel.Coin",
    .tp_basicsize = sizeof(CoinObject),
    .tp_dealloc = (destructor)Coin_dealloc,
    .tp_flags = Py_TPFLAGS_DEFAULT,
    .tp_doc = PyDoc_STR("An unspent transaction output with creation metadata."),
    .tp_getset = Coin_getset,
};

/* ------------------------------------------------------------------ */
/* TransactionSpentOutputs                                            */
/* ------------------------------------------------------------------ */

static void
TransactionSpentOutputs_dealloc(TransactionSpentOutputsObject* self)
{
    btck_transaction_spent_outputs_destroy(self->ptr);
    Py_TYPE(self)->tp_free((PyObject*)self);
}

static Py_ssize_t
TransactionSpentOutputs_length(TransactionSpentOutputsObject* self)
{
    return (Py_ssize_t)btck_transaction_spent_outputs_count(self->ptr);
}

static PyObject*
TransactionSpentOutputs_getitem(TransactionSpentOutputsObject* self, Py_ssize_t index)
{
    Py_ssize_t count = (Py_ssize_t)btck_transaction_spent_outputs_count(self->ptr);
    if (index < 0) {
        index += count;
    }
    if (index < 0 || index >= count) {
        PyErr_SetString(PyExc_IndexError, "coin index out of range");
        return NULL;
    }
    const btck_Coin* view =
        btck_transaction_spent_outputs_get_coin_at(self->ptr, (size_t)index);
    return Coin_wrap(btck_coin_copy(view));
}

static PySequenceMethods TransactionSpentOutputs_as_sequence = {
    .sq_length = (lenfunc)TransactionSpentOutputs_length,
    .sq_item = (ssizeargfunc)TransactionSpentOutputs_getitem,
};

static PyTypeObject TransactionSpentOutputsType = {
    .ob_base = PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "pybitcoinkernel._bitcoinkernel.TransactionSpentOutputs",
    .tp_basicsize = sizeof(TransactionSpentOutputsObject),
    .tp_dealloc = (destructor)TransactionSpentOutputs_dealloc,
    .tp_flags = Py_TPFLAGS_DEFAULT,
    .tp_doc = PyDoc_STR("The coins spent by one transaction, ordered like its inputs.\n"
                        "Acts as a sequence of Coin objects."),
    .tp_as_sequence = &TransactionSpentOutputs_as_sequence,
};

/* ------------------------------------------------------------------ */
/* BlockSpentOutputs                                                  */
/* ------------------------------------------------------------------ */

static void
BlockSpentOutputs_dealloc(BlockSpentOutputsObject* self)
{
    btck_block_spent_outputs_destroy(self->ptr);
    Py_TYPE(self)->tp_free((PyObject*)self);
}

static Py_ssize_t
BlockSpentOutputs_length(BlockSpentOutputsObject* self)
{
    return (Py_ssize_t)btck_block_spent_outputs_count(self->ptr);
}

static PyObject*
BlockSpentOutputs_getitem(BlockSpentOutputsObject* self, Py_ssize_t index)
{
    Py_ssize_t count = (Py_ssize_t)btck_block_spent_outputs_count(self->ptr);
    if (index < 0) {
        index += count;
    }
    if (index < 0 || index >= count) {
        PyErr_SetString(PyExc_IndexError, "transaction spent outputs index out of range");
        return NULL;
    }
    const btck_TransactionSpentOutputs* view =
        btck_block_spent_outputs_get_transaction_spent_outputs_at(self->ptr, (size_t)index);
    return TransactionSpentOutputs_wrap(btck_transaction_spent_outputs_copy(view));
}

static PySequenceMethods BlockSpentOutputs_as_sequence = {
    .sq_length = (lenfunc)BlockSpentOutputs_length,
    .sq_item = (ssizeargfunc)BlockSpentOutputs_getitem,
};

static PyTypeObject BlockSpentOutputsType = {
    .ob_base = PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "pybitcoinkernel._bitcoinkernel.BlockSpentOutputs",
    .tp_basicsize = sizeof(BlockSpentOutputsObject),
    .tp_dealloc = (destructor)BlockSpentOutputs_dealloc,
    .tp_flags = Py_TPFLAGS_DEFAULT,
    .tp_doc = PyDoc_STR("All coins spent by a block's transactions, without the\n"
                        "coinbase, ordered like the block's transactions. Acts as a\n"
                        "sequence of TransactionSpentOutputs."),
    .tp_as_sequence = &BlockSpentOutputs_as_sequence,
};

/* ------------------------------------------------------------------ */
/* LoggingConnection                                                  */
/* ------------------------------------------------------------------ */

static void
LoggingConnection_dealloc(LoggingConnectionObject* self)
{
    if (self->ptr != NULL) {
        btck_LoggingConnection* ptr = self->ptr;
        self->ptr = NULL;
        Py_BEGIN_ALLOW_THREADS
        btck_logging_connection_destroy(ptr);
        Py_END_ALLOW_THREADS
    }
    Py_TYPE(self)->tp_free((PyObject*)self);
}

static PyObject*
LoggingConnection_new(PyTypeObject* type, PyObject* args, PyObject* kwds)
{
    PyObject* callback;
    static char* kwlist[] = {"callback", NULL};
    if (!PyArg_ParseTupleAndKeywords(args, kwds, "O", kwlist, &callback)) {
        return NULL;
    }
    if (!PyCallable_Check(callback)) {
        PyErr_SetString(PyExc_TypeError, "callback must be callable");
        return NULL;
    }
    Py_INCREF(callback);
    btck_LoggingConnection* ptr =
        btck_logging_connection_create(log_cb, callback, pyobject_destroy_cb);
    if (ptr == NULL) {
        Py_DECREF(callback);
        PyErr_SetString(KernelError, "failed to create logging connection");
        return NULL;
    }
    LoggingConnectionObject* self = (LoggingConnectionObject*)type->tp_alloc(type, 0);
    if (self == NULL) {
        btck_logging_connection_destroy(ptr);
        return NULL;
    }
    self->ptr = ptr;
    return (PyObject*)self;
}

static PyObject*
LoggingConnection_close(LoggingConnectionObject* self, PyObject* Py_UNUSED(ignored))
{
    if (self->ptr != NULL) {
        btck_LoggingConnection* ptr = self->ptr;
        self->ptr = NULL;
        Py_BEGIN_ALLOW_THREADS
        btck_logging_connection_destroy(ptr);
        Py_END_ALLOW_THREADS
    }
    Py_RETURN_NONE;
}

static PyMethodDef LoggingConnection_methods[] = {
    {"close", (PyCFunction)LoggingConnection_close, METH_NOARGS,
     "Stop logging and destroy the connection."},
    {NULL, NULL, 0, NULL},
};

static PyTypeObject LoggingConnectionType = {
    .ob_base = PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "pybitcoinkernel._bitcoinkernel.LoggingConnection",
    .tp_basicsize = sizeof(LoggingConnectionObject),
    .tp_dealloc = (destructor)LoggingConnection_dealloc,
    .tp_flags = Py_TPFLAGS_DEFAULT,
    .tp_doc = PyDoc_STR("LoggingConnection(callback) - route kernel log messages to\n"
                        "callback(message: str)."),
    .tp_new = LoggingConnection_new,
    .tp_methods = LoggingConnection_methods,
};

/* ------------------------------------------------------------------ */
/* ChainParameters                                                    */
/* ------------------------------------------------------------------ */

static void
ChainParameters_dealloc(ChainParametersObject* self)
{
    btck_chain_parameters_destroy(self->ptr);
    Py_TYPE(self)->tp_free((PyObject*)self);
}

static PyObject*
ChainParameters_new(PyTypeObject* type, PyObject* args, PyObject* kwds)
{
    int chain_type;
    static char* kwlist[] = {"chain_type", NULL};
    if (!PyArg_ParseTupleAndKeywords(args, kwds, "i", kwlist, &chain_type)) {
        return NULL;
    }
    if (chain_type < 0 || chain_type > btck_ChainType_REGTEST) {
        PyErr_SetString(PyExc_ValueError, "invalid chain type");
        return NULL;
    }
    btck_ChainParameters* ptr = btck_chain_parameters_create((btck_ChainType)chain_type);
    if (ptr == NULL) {
        PyErr_SetString(KernelError, "failed to create chain parameters");
        return NULL;
    }
    ChainParametersObject* self = (ChainParametersObject*)type->tp_alloc(type, 0);
    if (self == NULL) {
        btck_chain_parameters_destroy(ptr);
        return NULL;
    }
    self->ptr = ptr;
    return (PyObject*)self;
}

static PyTypeObject ChainParametersType = {
    .ob_base = PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "pybitcoinkernel._bitcoinkernel.ChainParameters",
    .tp_basicsize = sizeof(ChainParametersObject),
    .tp_dealloc = (destructor)ChainParameters_dealloc,
    .tp_flags = Py_TPFLAGS_DEFAULT,
    .tp_doc = PyDoc_STR("ChainParameters(chain_type: int) - parameters for one of the\n"
                        "CHAIN_TYPE_* networks."),
    .tp_new = ChainParameters_new,
};

/* ------------------------------------------------------------------ */
/* ContextOptions                                                     */
/* ------------------------------------------------------------------ */

static void
ContextOptions_dealloc(ContextOptionsObject* self)
{
    btck_context_options_destroy(self->ptr);
    Py_TYPE(self)->tp_free((PyObject*)self);
}

static PyObject*
ContextOptions_new(PyTypeObject* type, PyObject* args, PyObject* kwds)
{
    if (!PyArg_ParseTuple(args, "")) {
        return NULL;
    }
    btck_ContextOptions* ptr = btck_context_options_create();
    if (ptr == NULL) {
        PyErr_SetString(KernelError, "failed to create context options");
        return NULL;
    }
    ContextOptionsObject* self = (ContextOptionsObject*)type->tp_alloc(type, 0);
    if (self == NULL) {
        btck_context_options_destroy(ptr);
        return NULL;
    }
    self->ptr = ptr;
    return (PyObject*)self;
}

static PyObject*
ContextOptions_set_chainparams(ContextOptionsObject* self, PyObject* arg)
{
    if (!PyObject_TypeCheck(arg, &ChainParametersType)) {
        PyErr_SetString(PyExc_TypeError, "expected ChainParameters");
        return NULL;
    }
    btck_context_options_set_chainparams(self->ptr, ((ChainParametersObject*)arg)->ptr);
    Py_RETURN_NONE;
}

static PyObject*
ContextOptions_set_notifications(ContextOptionsObject* self, PyObject* handler)
{
    Py_INCREF(handler);
    btck_NotificationInterfaceCallbacks cbs = {
        .user_data = handler,
        .user_data_destroy = pyobject_destroy_cb,
        .block_tip = notify_block_tip_cb,
        .header_tip = notify_header_tip_cb,
        .progress = notify_progress_cb,
        .warning_set = notify_warning_set_cb,
        .warning_unset = notify_warning_unset_cb,
        .flush_error = notify_flush_error_cb,
        .fatal_error = notify_fatal_error_cb,
    };
    btck_context_options_set_notifications(self->ptr, cbs);
    Py_RETURN_NONE;
}

static PyObject*
ContextOptions_set_validation_interface(ContextOptionsObject* self, PyObject* handler)
{
    Py_INCREF(handler);
    btck_ValidationInterfaceCallbacks cbs = {
        .user_data = handler,
        .user_data_destroy = pyobject_destroy_cb,
        .block_checked = vi_block_checked_cb,
        .pow_valid_block = vi_pow_valid_block_cb,
        .block_connected = vi_block_connected_cb,
        .block_disconnected = vi_block_disconnected_cb,
    };
    btck_context_options_set_validation_interface(self->ptr, cbs);
    Py_RETURN_NONE;
}

static PyMethodDef ContextOptions_methods[] = {
    {"set_chainparams", (PyCFunction)ContextOptions_set_chainparams, METH_O,
     "Select the chain the context will be configured for."},
    {"set_notifications", (PyCFunction)ContextOptions_set_notifications, METH_O,
     "Register a notification handler object. Optional methods:\n"
     "block_tip(state, entry, progress), header_tip(state, height, ts, presync),\n"
     "progress(title, percent, resume_possible), warning_set(warning, message),\n"
     "warning_unset(warning), flush_error(message), fatal_error(message)."},
    {"set_validation_interface", (PyCFunction)ContextOptions_set_validation_interface, METH_O,
     "Register a validation handler object. Optional methods:\n"
     "block_checked(block, state), pow_valid_block(block, entry),\n"
     "block_connected(block, entry), block_disconnected(block, entry)."},
    {NULL, NULL, 0, NULL},
};

static PyTypeObject ContextOptionsType = {
    .ob_base = PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "pybitcoinkernel._bitcoinkernel.ContextOptions",
    .tp_basicsize = sizeof(ContextOptionsObject),
    .tp_dealloc = (destructor)ContextOptions_dealloc,
    .tp_flags = Py_TPFLAGS_DEFAULT,
    .tp_doc = PyDoc_STR("Options for creating a kernel Context."),
    .tp_new = ContextOptions_new,
    .tp_methods = ContextOptions_methods,
};

/* ------------------------------------------------------------------ */
/* Context                                                            */
/* ------------------------------------------------------------------ */

static void
Context_dealloc(ContextObject* self)
{
    if (self->ptr != NULL) {
        btck_Context* ptr = self->ptr;
        self->ptr = NULL;
        Py_BEGIN_ALLOW_THREADS
        btck_context_destroy(ptr);
        Py_END_ALLOW_THREADS
    }
    Py_TYPE(self)->tp_free((PyObject*)self);
}

static PyObject*
Context_new(PyTypeObject* type, PyObject* args, PyObject* kwds)
{
    PyObject* options_obj = Py_None;
    static char* kwlist[] = {"options", NULL};
    if (!PyArg_ParseTupleAndKeywords(args, kwds, "|O", kwlist, &options_obj)) {
        return NULL;
    }
    const btck_ContextOptions* options = NULL;
    if (options_obj != Py_None) {
        if (!PyObject_TypeCheck(options_obj, &ContextOptionsType)) {
            PyErr_SetString(PyExc_TypeError, "options must be ContextOptions or None");
            return NULL;
        }
        options = ((ContextOptionsObject*)options_obj)->ptr;
    }
    btck_Context* ptr;
    Py_BEGIN_ALLOW_THREADS
    ptr = btck_context_create(options);
    Py_END_ALLOW_THREADS
    if (ptr == NULL) {
        PyErr_SetString(KernelError, "failed to create kernel context");
        return NULL;
    }
    ContextObject* self = (ContextObject*)type->tp_alloc(type, 0);
    if (self == NULL) {
        btck_context_destroy(ptr);
        return NULL;
    }
    self->ptr = ptr;
    return (PyObject*)self;
}

static PyObject*
Context_interrupt(ContextObject* self, PyObject* Py_UNUSED(ignored))
{
    CHECK_PTR(self);
    int rc = btck_context_interrupt(self->ptr);
    return PyBool_FromLong(rc == 0);
}

static PyMethodDef Context_methods[] = {
    {"interrupt", (PyCFunction)Context_interrupt, METH_NOARGS,
     "Interrupt long-running validation functions. Returns True on success."},
    {NULL, NULL, 0, NULL},
};

static PyTypeObject ContextType = {
    .ob_base = PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "pybitcoinkernel._bitcoinkernel.Context",
    .tp_basicsize = sizeof(ContextObject),
    .tp_dealloc = (destructor)Context_dealloc,
    .tp_flags = Py_TPFLAGS_DEFAULT,
    .tp_doc = PyDoc_STR("Context(options: ContextOptions | None = None)\n\n"
                        "A kernel context holding chain parameters and callbacks."),
    .tp_new = Context_new,
    .tp_methods = Context_methods,
};

/* ------------------------------------------------------------------ */
/* ChainstateManagerOptions                                           */
/* ------------------------------------------------------------------ */

static void
ChainstateManagerOptions_dealloc(ChainstateManagerOptionsObject* self)
{
    btck_chainstate_manager_options_destroy(self->ptr);
    Py_XDECREF(self->context);
    Py_TYPE(self)->tp_free((PyObject*)self);
}

static PyObject*
ChainstateManagerOptions_new(PyTypeObject* type, PyObject* args, PyObject* kwds)
{
    PyObject* context_obj;
    PyObject* data_dir_obj;
    PyObject* blocks_dir_obj;
    static char* kwlist[] = {"context", "data_dir", "blocks_dir", NULL};
    if (!PyArg_ParseTupleAndKeywords(args, kwds, "O!O&O&", kwlist,
                                     &ContextType, &context_obj,
                                     PyUnicode_FSConverter, &data_dir_obj,
                                     PyUnicode_FSConverter, &blocks_dir_obj)) {
        return NULL;
    }

    char* data_dir;
    Py_ssize_t data_dir_len;
    char* blocks_dir;
    Py_ssize_t blocks_dir_len;
    if (PyBytes_AsStringAndSize(data_dir_obj, &data_dir, &data_dir_len) < 0 ||
        PyBytes_AsStringAndSize(blocks_dir_obj, &blocks_dir, &blocks_dir_len) < 0) {
        Py_DECREF(data_dir_obj);
        Py_DECREF(blocks_dir_obj);
        return NULL;
    }

    btck_ChainstateManagerOptions* ptr = btck_chainstate_manager_options_create(
        ((ContextObject*)context_obj)->ptr,
        data_dir, (size_t)data_dir_len,
        blocks_dir, (size_t)blocks_dir_len);
    Py_DECREF(data_dir_obj);
    Py_DECREF(blocks_dir_obj);
    if (ptr == NULL) {
        PyErr_SetString(KernelError, "failed to create chainstate manager options");
        return NULL;
    }
    ChainstateManagerOptionsObject* self =
        (ChainstateManagerOptionsObject*)type->tp_alloc(type, 0);
    if (self == NULL) {
        btck_chainstate_manager_options_destroy(ptr);
        return NULL;
    }
    self->ptr = ptr;
    self->context = context_obj;
    Py_INCREF(context_obj);
    return (PyObject*)self;
}

static PyObject*
ChainstateManagerOptions_set_worker_threads(ChainstateManagerOptionsObject* self, PyObject* arg)
{
    long n = PyLong_AsLong(arg);
    if (n == -1 && PyErr_Occurred()) {
        return NULL;
    }
    btck_chainstate_manager_options_set_worker_threads_num(self->ptr, (int)n);
    Py_RETURN_NONE;
}

static PyObject*
ChainstateManagerOptions_set_wipe_dbs(ChainstateManagerOptionsObject* self,
                                      PyObject* args, PyObject* kwds)
{
    int wipe_block_tree_db;
    int wipe_chainstate_db;
    static char* kwlist[] = {"wipe_block_tree_db", "wipe_chainstate_db", NULL};
    if (!PyArg_ParseTupleAndKeywords(args, kwds, "pp", kwlist,
                                     &wipe_block_tree_db, &wipe_chainstate_db)) {
        return NULL;
    }
    int rc = btck_chainstate_manager_options_set_wipe_dbs(
        self->ptr, wipe_block_tree_db, wipe_chainstate_db);
    if (rc != 0) {
        PyErr_SetString(PyExc_ValueError,
                        "invalid wipe combination: wiping the block tree db requires "
                        "wiping the chainstate db too");
        return NULL;
    }
    Py_RETURN_NONE;
}

static PyObject*
ChainstateManagerOptions_set_block_tree_db_in_memory(ChainstateManagerOptionsObject* self,
                                                     PyObject* arg)
{
    int value = PyObject_IsTrue(arg);
    if (value < 0) {
        return NULL;
    }
    btck_chainstate_manager_options_update_block_tree_db_in_memory(self->ptr, value);
    Py_RETURN_NONE;
}

static PyObject*
ChainstateManagerOptions_set_chainstate_db_in_memory(ChainstateManagerOptionsObject* self,
                                                     PyObject* arg)
{
    int value = PyObject_IsTrue(arg);
    if (value < 0) {
        return NULL;
    }
    btck_chainstate_manager_options_update_chainstate_db_in_memory(self->ptr, value);
    Py_RETURN_NONE;
}

static PyMethodDef ChainstateManagerOptions_methods[] = {
    {"set_worker_threads", (PyCFunction)ChainstateManagerOptions_set_worker_threads, METH_O,
     "Set the number of validation worker threads (clamped to 0..15)."},
    {"set_wipe_dbs", (PyCFunction)(void (*)(void))ChainstateManagerOptions_set_wipe_dbs,
     METH_VARARGS | METH_KEYWORDS,
     "set_wipe_dbs(wipe_block_tree_db, wipe_chainstate_db)\n\n"
     "Wipe databases on load; combine with import_blocks() to reindex."},
    {"set_block_tree_db_in_memory",
     (PyCFunction)ChainstateManagerOptions_set_block_tree_db_in_memory, METH_O,
     "Keep the block tree db in memory instead of on disk."},
    {"set_chainstate_db_in_memory",
     (PyCFunction)ChainstateManagerOptions_set_chainstate_db_in_memory, METH_O,
     "Keep the chainstate db in memory instead of on disk."},
    {NULL, NULL, 0, NULL},
};

static PyTypeObject ChainstateManagerOptionsType = {
    .ob_base = PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "pybitcoinkernel._bitcoinkernel.ChainstateManagerOptions",
    .tp_basicsize = sizeof(ChainstateManagerOptionsObject),
    .tp_dealloc = (destructor)ChainstateManagerOptions_dealloc,
    .tp_flags = Py_TPFLAGS_DEFAULT,
    .tp_doc = PyDoc_STR("ChainstateManagerOptions(context, data_dir, blocks_dir)"),
    .tp_new = ChainstateManagerOptions_new,
    .tp_methods = ChainstateManagerOptions_methods,
};

/* ------------------------------------------------------------------ */
/* BlockTreeEntry                                                     */
/* ------------------------------------------------------------------ */

static void
BlockTreeEntry_dealloc(BlockTreeEntryObject* self)
{
    Py_XDECREF(self->owner);
    Py_TYPE(self)->tp_free((PyObject*)self);
}

static PyObject*
BlockTreeEntry_get_height(BlockTreeEntryObject* self, void* closure)
{
    CHECK_VIEW(self);
    return PyLong_FromLong(btck_block_tree_entry_get_height(self->ptr));
}

static PyObject*
BlockTreeEntry_get_block_hash(BlockTreeEntryObject* self, void* closure)
{
    CHECK_VIEW(self);
    const btck_BlockHash* view = btck_block_tree_entry_get_block_hash(self->ptr);
    return BlockHash_wrap(btck_block_hash_copy(view));
}

static PyObject*
BlockTreeEntry_get_header(BlockTreeEntryObject* self, void* closure)
{
    CHECK_VIEW(self);
    return BlockHeader_wrap(btck_block_tree_entry_get_block_header(self->ptr));
}

static PyObject*
BlockTreeEntry_get_prev(BlockTreeEntryObject* self, void* closure)
{
    CHECK_VIEW(self);
    const btck_BlockTreeEntry* prev = btck_block_tree_entry_get_previous(self->ptr);
    if (prev == NULL) {
        Py_RETURN_NONE;
    }
    return BlockTreeEntry_wrap(prev, self->owner);
}

static PyObject*
BlockTreeEntry_richcompare(PyObject* a, PyObject* b, int op)
{
    if ((op != Py_EQ && op != Py_NE) ||
        !PyObject_TypeCheck(a, &BlockTreeEntryType) ||
        !PyObject_TypeCheck(b, &BlockTreeEntryType)) {
        Py_RETURN_NOTIMPLEMENTED;
    }
    if (!view_owner_alive(((BlockTreeEntryObject*)a)->owner) ||
        !view_owner_alive(((BlockTreeEntryObject*)b)->owner)) {
        return NULL;
    }
    int eq = btck_block_tree_entry_equals(((BlockTreeEntryObject*)a)->ptr,
                                          ((BlockTreeEntryObject*)b)->ptr);
    if (op == Py_NE) {
        eq = !eq;
    }
    return PyBool_FromLong(eq);
}

static PyObject*
BlockTreeEntry_repr(BlockTreeEntryObject* self)
{
    CHECK_VIEW(self);
    return PyUnicode_FromFormat("<BlockTreeEntry height=%d>",
                                (int)btck_block_tree_entry_get_height(self->ptr));
}

static PyGetSetDef BlockTreeEntry_getset[] = {
    {"height", (getter)BlockTreeEntry_get_height, NULL, "Block height.", NULL},
    {"block_hash", (getter)BlockTreeEntry_get_block_hash, NULL,
     "Hash of the block this entry points to.", NULL},
    {"header", (getter)BlockTreeEntry_get_header, NULL,
     "The block header of this entry.", NULL},
    {"prev", (getter)BlockTreeEntry_get_prev, NULL,
     "The previous (parent) entry, or None for the genesis block.", NULL},
    {NULL, NULL, NULL, NULL, NULL},
};

static PyTypeObject BlockTreeEntryType = {
    .ob_base = PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "pybitcoinkernel._bitcoinkernel.BlockTreeEntry",
    .tp_basicsize = sizeof(BlockTreeEntryObject),
    .tp_dealloc = (destructor)BlockTreeEntry_dealloc,
    .tp_flags = Py_TPFLAGS_DEFAULT,
    .tp_doc = PyDoc_STR("An entry in the chainstate manager's block index. Valid for\n"
                        "the lifetime of the chainstate manager it came from."),
    .tp_getset = BlockTreeEntry_getset,
    .tp_richcompare = BlockTreeEntry_richcompare,
    .tp_repr = (reprfunc)BlockTreeEntry_repr,
};

/* ------------------------------------------------------------------ */
/* Chain                                                              */
/* ------------------------------------------------------------------ */

static void
Chain_dealloc(ChainObject* self)
{
    Py_XDECREF(self->owner);
    Py_TYPE(self)->tp_free((PyObject*)self);
}

static PyObject*
Chain_get_height(ChainObject* self, void* closure)
{
    CHECK_VIEW(self);
    return PyLong_FromLong(btck_chain_get_height(self->ptr));
}

static Py_ssize_t
Chain_length(ChainObject* self)
{
    if (!view_owner_alive(self->owner)) {
        return -1;
    }
    return (Py_ssize_t)btck_chain_get_height(self->ptr) + 1;
}

static PyObject*
Chain_getitem(ChainObject* self, Py_ssize_t index)
{
    CHECK_VIEW(self);
    Py_ssize_t count = (Py_ssize_t)btck_chain_get_height(self->ptr) + 1;
    if (index < 0) {
        index += count;
    }
    if (index < 0 || index >= count) {
        PyErr_SetString(PyExc_IndexError, "block height out of range");
        return NULL;
    }
    const btck_BlockTreeEntry* entry = btck_chain_get_by_height(self->ptr, (int)index);
    if (entry == NULL) {
        PyErr_SetString(PyExc_IndexError, "block height out of range");
        return NULL;
    }
    return BlockTreeEntry_wrap(entry, self->owner);
}

static int
Chain_contains_impl(ChainObject* self, PyObject* value)
{
    if (!view_owner_alive(self->owner)) {
        return -1;
    }
    if (!PyObject_TypeCheck(value, &BlockTreeEntryType)) {
        PyErr_SetString(PyExc_TypeError, "expected BlockTreeEntry");
        return -1;
    }
    return btck_chain_contains(self->ptr, ((BlockTreeEntryObject*)value)->ptr) ? 1 : 0;
}

static PyObject*
Chain_contains(ChainObject* self, PyObject* value)
{
    int rc = Chain_contains_impl(self, value);
    if (rc < 0) {
        return NULL;
    }
    return PyBool_FromLong(rc);
}

static PyObject*
Chain_tip(ChainObject* self, PyObject* Py_UNUSED(ignored))
{
    CHECK_VIEW(self);
    int32_t height = btck_chain_get_height(self->ptr);
    const btck_BlockTreeEntry* entry = btck_chain_get_by_height(self->ptr, (int)height);
    if (entry == NULL) {
        PyErr_SetString(KernelError, "failed to retrieve chain tip");
        return NULL;
    }
    return BlockTreeEntry_wrap(entry, self->owner);
}

static PySequenceMethods Chain_as_sequence = {
    .sq_length = (lenfunc)Chain_length,
    .sq_item = (ssizeargfunc)Chain_getitem,
    .sq_contains = (objobjproc)Chain_contains_impl,
};

static PyMethodDef Chain_methods[] = {
    {"contains", (PyCFunction)Chain_contains, METH_O,
     "contains(entry) -> bool - whether the entry is part of this chain."},
    {"tip", (PyCFunction)Chain_tip, METH_NOARGS,
     "tip() -> BlockTreeEntry - the entry at the current chain tip."},
    {NULL, NULL, 0, NULL},
};

static PyGetSetDef Chain_getset[] = {
    {"height", (getter)Chain_get_height, NULL,
     "Height of the chain tip (genesis is height 0).", NULL},
    {NULL, NULL, NULL, NULL, NULL},
};

static PyTypeObject ChainType_ = {
    .ob_base = PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "pybitcoinkernel._bitcoinkernel.Chain",
    .tp_basicsize = sizeof(ChainObject),
    .tp_dealloc = (destructor)Chain_dealloc,
    .tp_flags = Py_TPFLAGS_DEFAULT,
    .tp_doc = PyDoc_STR("A live view of the current best chain. Acts as a sequence of\n"
                        "BlockTreeEntry indexed by height: chain[0] is genesis,\n"
                        "chain[-1] the tip."),
    .tp_getset = Chain_getset,
    .tp_methods = Chain_methods,
    .tp_as_sequence = &Chain_as_sequence,
};

/* ------------------------------------------------------------------ */
/* ChainstateManager                                                  */
/* ------------------------------------------------------------------ */

static void
ChainstateManager_close_impl(ChainstateManagerObject* self)
{
    if (self->ptr != NULL) {
        btck_ChainstateManager* ptr = self->ptr;
        self->ptr = NULL;
        /* Destruction flushes state to disk and may fire callbacks that
         * need the GIL from other threads, so release it. */
        Py_BEGIN_ALLOW_THREADS
        btck_chainstate_manager_destroy(ptr);
        Py_END_ALLOW_THREADS
    }
}

static void
ChainstateManager_dealloc(ChainstateManagerObject* self)
{
    ChainstateManager_close_impl(self);
    Py_XDECREF(self->context);
    Py_TYPE(self)->tp_free((PyObject*)self);
}

static PyObject*
ChainstateManager_new(PyTypeObject* type, PyObject* args, PyObject* kwds)
{
    PyObject* options_obj;
    static char* kwlist[] = {"options", NULL};
    if (!PyArg_ParseTupleAndKeywords(args, kwds, "O!", kwlist,
                                     &ChainstateManagerOptionsType, &options_obj)) {
        return NULL;
    }
    ChainstateManagerOptionsObject* options = (ChainstateManagerOptionsObject*)options_obj;
    btck_ChainstateManager* ptr;
    Py_BEGIN_ALLOW_THREADS
    ptr = btck_chainstate_manager_create(options->ptr);
    Py_END_ALLOW_THREADS
    if (ptr == NULL) {
        PyErr_SetString(KernelError, "failed to create chainstate manager");
        return NULL;
    }
    ChainstateManagerObject* self = (ChainstateManagerObject*)type->tp_alloc(type, 0);
    if (self == NULL) {
        Py_BEGIN_ALLOW_THREADS
        btck_chainstate_manager_destroy(ptr);
        Py_END_ALLOW_THREADS
        return NULL;
    }
    self->ptr = ptr;
    self->context = options->context;
    Py_XINCREF(self->context);
    return (PyObject*)self;
}

static PyObject*
ChainstateManager_close(ChainstateManagerObject* self, PyObject* Py_UNUSED(ignored))
{
    ChainstateManager_close_impl(self);
    Py_RETURN_NONE;
}

static PyObject*
ChainstateManager_enter(ChainstateManagerObject* self, PyObject* Py_UNUSED(ignored))
{
    CHECK_PTR(self);
    Py_INCREF(self);
    return (PyObject*)self;
}

static PyObject*
ChainstateManager_exit(ChainstateManagerObject* self, PyObject* args)
{
    ChainstateManager_close_impl(self);
    Py_RETURN_FALSE;
}

static PyObject*
ChainstateManager_get_best_entry(ChainstateManagerObject* self, PyObject* Py_UNUSED(ignored))
{
    CHECK_PTR(self);
    const btck_BlockTreeEntry* entry = btck_chainstate_manager_get_best_entry(self->ptr);
    return BlockTreeEntry_wrap(entry, (PyObject*)self);
}

static PyObject*
ChainstateManager_get_active_chain(ChainstateManagerObject* self, PyObject* Py_UNUSED(ignored))
{
    CHECK_PTR(self);
    const btck_Chain* chain = btck_chainstate_manager_get_active_chain(self->ptr);
    ChainObject* obj = PyObject_New(ChainObject, &ChainType_);
    if (obj == NULL) {
        return NULL;
    }
    obj->ptr = chain;
    obj->owner = (PyObject*)self;
    Py_INCREF(self);
    return (PyObject*)obj;
}

static PyObject*
ChainstateManager_get_block_tree_entry_by_hash(ChainstateManagerObject* self, PyObject* arg)
{
    CHECK_PTR(self);
    if (!PyObject_TypeCheck(arg, &BlockHashType)) {
        PyErr_SetString(PyExc_TypeError, "expected BlockHash");
        return NULL;
    }
    const btck_BlockTreeEntry* entry = btck_chainstate_manager_get_block_tree_entry_by_hash(
        self->ptr, ((BlockHashObject*)arg)->ptr);
    if (entry == NULL) {
        Py_RETURN_NONE;
    }
    return BlockTreeEntry_wrap(entry, (PyObject*)self);
}

static PyObject*
ChainstateManager_process_block(ChainstateManagerObject* self, PyObject* arg)
{
    CHECK_PTR(self);
    if (!PyObject_TypeCheck(arg, &BlockType)) {
        PyErr_SetString(PyExc_TypeError, "expected Block");
        return NULL;
    }
    btck_Block* block = ((BlockObject*)arg)->ptr;
    int new_block = 0;
    int rc;
    Py_BEGIN_ALLOW_THREADS
    rc = btck_chainstate_manager_process_block(self->ptr, block, &new_block);
    Py_END_ALLOW_THREADS
    return Py_BuildValue("(OO)", rc == 0 ? Py_True : Py_False,
                         new_block ? Py_True : Py_False);
}

static PyObject*
ChainstateManager_process_block_header(ChainstateManagerObject* self, PyObject* arg)
{
    CHECK_PTR(self);
    if (!PyObject_TypeCheck(arg, &BlockHeaderType)) {
        PyErr_SetString(PyExc_TypeError, "expected BlockHeader");
        return NULL;
    }
    btck_BlockHeader* header = ((BlockHeaderObject*)arg)->ptr;
    btck_BlockValidationState* state;
    Py_BEGIN_ALLOW_THREADS
    state = btck_chainstate_manager_process_block_header(self->ptr, header);
    Py_END_ALLOW_THREADS
    if (state == NULL) {
        PyErr_SetString(KernelError, "failed to process block header");
        return NULL;
    }
    return BlockValidationState_wrap(state);
}

static PyObject*
ChainstateManager_import_blocks(ChainstateManagerObject* self, PyObject* args, PyObject* kwds)
{
    CHECK_PTR(self);
    PyObject* paths_obj = Py_None;
    static char* kwlist[] = {"paths", NULL};
    if (!PyArg_ParseTupleAndKeywords(args, kwds, "|O", kwlist, &paths_obj)) {
        return NULL;
    }

    const char** paths_data = NULL;
    size_t* paths_lens = NULL;
    PyObject** encoded = NULL;
    size_t n_paths = 0;

    if (paths_obj != Py_None) {
        PyObject* seq = PySequence_Fast(paths_obj, "paths must be a sequence or None");
        if (seq == NULL) {
            return NULL;
        }
        n_paths = (size_t)PySequence_Fast_GET_SIZE(seq);
        if (n_paths > 0) {
            paths_data = PyMem_Malloc(n_paths * sizeof(*paths_data));
            paths_lens = PyMem_Malloc(n_paths * sizeof(*paths_lens));
            encoded = PyMem_Calloc(n_paths, sizeof(*encoded));
            if (paths_data == NULL || paths_lens == NULL || encoded == NULL) {
                PyMem_Free(paths_data);
                PyMem_Free(paths_lens);
                PyMem_Free(encoded);
                Py_DECREF(seq);
                return PyErr_NoMemory();
            }
            for (size_t i = 0; i < n_paths; i++) {
                PyObject* item = PySequence_Fast_GET_ITEM(seq, (Py_ssize_t)i);
                if (!PyUnicode_FSConverter(item, &encoded[i])) {
                    for (size_t j = 0; j < i; j++) {
                        Py_DECREF(encoded[j]);
                    }
                    PyMem_Free(paths_data);
                    PyMem_Free(paths_lens);
                    PyMem_Free(encoded);
                    Py_DECREF(seq);
                    return NULL;
                }
                char* buf;
                Py_ssize_t len;
                PyBytes_AsStringAndSize(encoded[i], &buf, &len);
                paths_data[i] = buf;
                paths_lens[i] = (size_t)len;
            }
        }
        Py_DECREF(seq);
    }

    int rc;
    Py_BEGIN_ALLOW_THREADS
    rc = btck_chainstate_manager_import_blocks(self->ptr, paths_data, paths_lens, n_paths);
    Py_END_ALLOW_THREADS

    for (size_t i = 0; i < n_paths; i++) {
        Py_XDECREF(encoded[i]);
    }
    PyMem_Free(paths_data);
    PyMem_Free(paths_lens);
    PyMem_Free(encoded);

    if (rc != 0) {
        PyErr_SetString(KernelError, "importing blocks failed");
        return NULL;
    }
    Py_RETURN_NONE;
}

static PyObject*
ChainstateManager_read_block(ChainstateManagerObject* self, PyObject* arg)
{
    CHECK_PTR(self);
    if (!PyObject_TypeCheck(arg, &BlockTreeEntryType)) {
        PyErr_SetString(PyExc_TypeError, "expected BlockTreeEntry");
        return NULL;
    }
    const btck_BlockTreeEntry* entry = ((BlockTreeEntryObject*)arg)->ptr;
    btck_Block* block;
    Py_BEGIN_ALLOW_THREADS
    block = btck_block_read(self->ptr, entry);
    Py_END_ALLOW_THREADS
    if (block == NULL) {
        PyErr_SetString(KernelError, "failed to read block from disk");
        return NULL;
    }
    return Block_wrap(block);
}

static PyObject*
ChainstateManager_read_block_spent_outputs(ChainstateManagerObject* self, PyObject* arg)
{
    CHECK_PTR(self);
    if (!PyObject_TypeCheck(arg, &BlockTreeEntryType)) {
        PyErr_SetString(PyExc_TypeError, "expected BlockTreeEntry");
        return NULL;
    }
    const btck_BlockTreeEntry* entry = ((BlockTreeEntryObject*)arg)->ptr;
    btck_BlockSpentOutputs* spent;
    Py_BEGIN_ALLOW_THREADS
    spent = btck_block_spent_outputs_read(self->ptr, entry);
    Py_END_ALLOW_THREADS
    if (spent == NULL) {
        PyErr_SetString(KernelError, "failed to read block spent outputs from disk");
        return NULL;
    }
    BlockSpentOutputsObject* obj = PyObject_New(BlockSpentOutputsObject, &BlockSpentOutputsType);
    if (obj == NULL) {
        btck_block_spent_outputs_destroy(spent);
        return NULL;
    }
    obj->ptr = spent;
    return (PyObject*)obj;
}

static PyMethodDef ChainstateManager_methods[] = {
    {"close", (PyCFunction)ChainstateManager_close, METH_NOARGS,
     "Flush state to disk and destroy the chainstate manager."},
    {"__enter__", (PyCFunction)ChainstateManager_enter, METH_NOARGS, NULL},
    {"__exit__", (PyCFunction)ChainstateManager_exit, METH_VARARGS, NULL},
    {"get_best_entry", (PyCFunction)ChainstateManager_get_best_entry, METH_NOARGS,
     "Return the block tree entry with the most cumulative proof of work."},
    {"get_active_chain", (PyCFunction)ChainstateManager_get_active_chain, METH_NOARGS,
     "Return a live view of the current best chain."},
    {"get_block_tree_entry_by_hash",
     (PyCFunction)ChainstateManager_get_block_tree_entry_by_hash, METH_O,
     "Look up a block tree entry by BlockHash. Returns None if unknown."},
    {"process_block", (PyCFunction)ChainstateManager_process_block, METH_O,
     "process_block(block) -> (accepted: bool, new: bool)\n\n"
     "Validate a block and, if valid, extend the chain with it. `accepted` is\n"
     "False if the block was invalid or could not be processed; `new` is True\n"
     "if the block was not processed before."},
    {"process_block_header", (PyCFunction)ChainstateManager_process_block_header, METH_O,
     "process_block_header(header) -> BlockValidationState\n\n"
     "Validate a block header and, if valid, add it to the block index."},
    {"import_blocks", (PyCFunction)(void (*)(void))ChainstateManager_import_blocks,
     METH_VARARGS | METH_KEYWORDS,
     "import_blocks(paths=None)\n\n"
     "Trigger a reindex (if wipe options were set) and/or import the given\n"
     "block files (blk*.dat paths)."},
    {"read_block", (PyCFunction)ChainstateManager_read_block, METH_O,
     "read_block(entry) -> Block - read the block for the entry from disk."},
    {"read_block_spent_outputs",
     (PyCFunction)ChainstateManager_read_block_spent_outputs, METH_O,
     "read_block_spent_outputs(entry) -> BlockSpentOutputs - read the undo\n"
     "data (spent coins) of the entry's block from disk."},
    {NULL, NULL, 0, NULL},
};

static PyTypeObject ChainstateManagerType = {
    .ob_base = PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "pybitcoinkernel._bitcoinkernel.ChainstateManager",
    .tp_basicsize = sizeof(ChainstateManagerObject),
    .tp_dealloc = (destructor)ChainstateManager_dealloc,
    .tp_flags = Py_TPFLAGS_DEFAULT,
    .tp_doc = PyDoc_STR("ChainstateManager(options: ChainstateManagerOptions)\n\n"
                        "The central object for validating blocks and reading chain\n"
                        "data. Usable as a context manager."),
    .tp_new = ChainstateManager_new,
    .tp_methods = ChainstateManager_methods,
};

/* ------------------------------------------------------------------ */
/* Module-level logging functions                                     */
/* ------------------------------------------------------------------ */

static PyObject*
mod_logging_disable(PyObject* module, PyObject* Py_UNUSED(ignored))
{
    btck_logging_disable();
    Py_RETURN_NONE;
}

static PyObject*
mod_logging_set_options(PyObject* module, PyObject* args, PyObject* kwds)
{
    int log_timestamps = 1;
    int log_time_micros = 0;
    int log_threadnames = 0;
    int log_sourcelocations = 0;
    int always_print_category_levels = 0;
    static char* kwlist[] = {"log_timestamps", "log_time_micros", "log_threadnames",
                             "log_sourcelocations", "always_print_category_levels", NULL};
    if (!PyArg_ParseTupleAndKeywords(args, kwds, "|ppppp", kwlist,
                                     &log_timestamps, &log_time_micros, &log_threadnames,
                                     &log_sourcelocations, &always_print_category_levels)) {
        return NULL;
    }
    btck_LoggingOptions options = {
        .log_timestamps = log_timestamps,
        .log_time_micros = log_time_micros,
        .log_threadnames = log_threadnames,
        .log_sourcelocations = log_sourcelocations,
        .always_print_category_levels = always_print_category_levels,
    };
    btck_logging_set_options(options);
    Py_RETURN_NONE;
}

static PyObject*
mod_logging_set_level_category(PyObject* module, PyObject* args, PyObject* kwds)
{
    int category;
    int level;
    static char* kwlist[] = {"category", "level", NULL};
    if (!PyArg_ParseTupleAndKeywords(args, kwds, "ii", kwlist, &category, &level)) {
        return NULL;
    }
    btck_logging_set_level_category((btck_LogCategory)category, (btck_LogLevel)level);
    Py_RETURN_NONE;
}

static PyObject*
mod_logging_enable_category(PyObject* module, PyObject* arg)
{
    long category = PyLong_AsLong(arg);
    if (category == -1 && PyErr_Occurred()) {
        return NULL;
    }
    btck_logging_enable_category((btck_LogCategory)category);
    Py_RETURN_NONE;
}

static PyObject*
mod_logging_disable_category(PyObject* module, PyObject* arg)
{
    long category = PyLong_AsLong(arg);
    if (category == -1 && PyErr_Occurred()) {
        return NULL;
    }
    btck_logging_disable_category((btck_LogCategory)category);
    Py_RETURN_NONE;
}

static PyMethodDef module_methods[] = {
    {"logging_disable", mod_logging_disable, METH_NOARGS,
     "Permanently disable the global internal logger."},
    {"logging_set_options", (PyCFunction)(void (*)(void))mod_logging_set_options,
     METH_VARARGS | METH_KEYWORDS,
     "logging_set_options(log_timestamps=True, log_time_micros=False,\n"
     "                    log_threadnames=False, log_sourcelocations=False,\n"
     "                    always_print_category_levels=False)"},
    {"logging_set_level_category", (PyCFunction)(void (*)(void))mod_logging_set_level_category,
     METH_VARARGS | METH_KEYWORDS,
     "logging_set_level_category(category, level) - set the log level for a\n"
     "LOG_CATEGORY_* (LOG_CATEGORY_ALL sets the global fallback)."},
    {"logging_enable_category", mod_logging_enable_category, METH_O,
     "Enable logging for a LOG_CATEGORY_*."},
    {"logging_disable_category", mod_logging_disable_category, METH_O,
     "Disable logging for a LOG_CATEGORY_*."},
    {NULL, NULL, 0, NULL},
};

/* ------------------------------------------------------------------ */
/* Module init                                                        */
/* ------------------------------------------------------------------ */

static struct PyModuleDef bitcoinkernel_module = {
    PyModuleDef_HEAD_INIT,
    .m_name = "pybitcoinkernel._bitcoinkernel",
    .m_doc = "Low-level CPython bindings for Bitcoin Core's libbitcoinkernel.",
    .m_size = -1,
    .m_methods = module_methods,
};

static int
add_type(PyObject* module, const char* name, PyTypeObject* type)
{
    if (PyType_Ready(type) < 0) {
        return -1;
    }
    Py_INCREF(type);
    if (PyModule_AddObject(module, name, (PyObject*)type) < 0) {
        Py_DECREF(type);
        return -1;
    }
    return 0;
}

PyMODINIT_FUNC
PyInit__bitcoinkernel(void)
{
    PyObject* module = PyModule_Create(&bitcoinkernel_module);
    if (module == NULL) {
        return NULL;
    }

    KernelError = PyErr_NewExceptionWithDoc(
        "pybitcoinkernel._bitcoinkernel.KernelError",
        "Raised when a libbitcoinkernel operation fails.", NULL, NULL);
    if (KernelError == NULL || PyModule_AddObject(module, "KernelError", KernelError) < 0) {
        Py_XDECREF(KernelError);
        Py_DECREF(module);
        return NULL;
    }

    if (add_type(module, "Transaction", &TransactionType) < 0 ||
        add_type(module, "ScriptPubkey", &ScriptPubkeyType) < 0 ||
        add_type(module, "TransactionOutput", &TransactionOutputType) < 0 ||
        add_type(module, "TransactionInput", &TransactionInputType) < 0 ||
        add_type(module, "TransactionOutPoint", &TransactionOutPointType) < 0 ||
        add_type(module, "Txid", &TxidType) < 0 ||
        add_type(module, "PrecomputedTransactionData", &PrecomputedTransactionDataType) < 0 ||
        add_type(module, "BlockHash", &BlockHashType) < 0 ||
        add_type(module, "BlockHeader", &BlockHeaderType) < 0 ||
        add_type(module, "Block", &BlockType) < 0 ||
        add_type(module, "BlockValidationState", &BlockValidationStateType) < 0 ||
        add_type(module, "LoggingConnection", &LoggingConnectionType) < 0 ||
        add_type(module, "ChainParameters", &ChainParametersType) < 0 ||
        add_type(module, "ContextOptions", &ContextOptionsType) < 0 ||
        add_type(module, "Context", &ContextType) < 0 ||
        add_type(module, "ChainstateManagerOptions", &ChainstateManagerOptionsType) < 0 ||
        add_type(module, "ChainstateManager", &ChainstateManagerType) < 0 ||
        add_type(module, "BlockTreeEntry", &BlockTreeEntryType) < 0 ||
        add_type(module, "Chain", &ChainType_) < 0 ||
        add_type(module, "BlockSpentOutputs", &BlockSpentOutputsType) < 0 ||
        add_type(module, "TransactionSpentOutputs", &TransactionSpentOutputsType) < 0 ||
        add_type(module, "Coin", &CoinType) < 0) {
        Py_DECREF(module);
        return NULL;
    }

#define ADD_INT(name, value)                                          \
    do {                                                              \
        if (PyModule_AddIntConstant(module, name, (long)(value)) < 0) { \
            Py_DECREF(module);                                        \
            return NULL;                                              \
        }                                                             \
    } while (0)

    /* Chain types */
    ADD_INT("CHAIN_TYPE_MAINNET", btck_ChainType_MAINNET);
    ADD_INT("CHAIN_TYPE_TESTNET", btck_ChainType_TESTNET);
    ADD_INT("CHAIN_TYPE_TESTNET_4", btck_ChainType_TESTNET_4);
    ADD_INT("CHAIN_TYPE_SIGNET", btck_ChainType_SIGNET);
    ADD_INT("CHAIN_TYPE_REGTEST", btck_ChainType_REGTEST);

    /* Script verification flags */
    ADD_INT("SCRIPT_FLAGS_VERIFY_NONE", btck_ScriptVerificationFlags_NONE);
    ADD_INT("SCRIPT_FLAGS_VERIFY_P2SH", btck_ScriptVerificationFlags_P2SH);
    ADD_INT("SCRIPT_FLAGS_VERIFY_DERSIG", btck_ScriptVerificationFlags_DERSIG);
    ADD_INT("SCRIPT_FLAGS_VERIFY_NULLDUMMY", btck_ScriptVerificationFlags_NULLDUMMY);
    ADD_INT("SCRIPT_FLAGS_VERIFY_CHECKLOCKTIMEVERIFY",
            btck_ScriptVerificationFlags_CHECKLOCKTIMEVERIFY);
    ADD_INT("SCRIPT_FLAGS_VERIFY_CHECKSEQUENCEVERIFY",
            btck_ScriptVerificationFlags_CHECKSEQUENCEVERIFY);
    ADD_INT("SCRIPT_FLAGS_VERIFY_WITNESS", btck_ScriptVerificationFlags_WITNESS);
    ADD_INT("SCRIPT_FLAGS_VERIFY_TAPROOT", btck_ScriptVerificationFlags_TAPROOT);
    ADD_INT("SCRIPT_FLAGS_VERIFY_ALL", btck_ScriptVerificationFlags_ALL);

    /* Validation modes */
    ADD_INT("VALIDATION_MODE_VALID", btck_ValidationMode_VALID);
    ADD_INT("VALIDATION_MODE_INVALID", btck_ValidationMode_INVALID);
    ADD_INT("VALIDATION_MODE_INTERNAL_ERROR", btck_ValidationMode_INTERNAL_ERROR);

    /* Block validation results */
    ADD_INT("BLOCK_VALIDATION_RESULT_UNSET", btck_BlockValidationResult_UNSET);
    ADD_INT("BLOCK_VALIDATION_RESULT_CONSENSUS", btck_BlockValidationResult_CONSENSUS);
    ADD_INT("BLOCK_VALIDATION_RESULT_CACHED_INVALID",
            btck_BlockValidationResult_CACHED_INVALID);
    ADD_INT("BLOCK_VALIDATION_RESULT_INVALID_HEADER",
            btck_BlockValidationResult_INVALID_HEADER);
    ADD_INT("BLOCK_VALIDATION_RESULT_MUTATED", btck_BlockValidationResult_MUTATED);
    ADD_INT("BLOCK_VALIDATION_RESULT_MISSING_PREV", btck_BlockValidationResult_MISSING_PREV);
    ADD_INT("BLOCK_VALIDATION_RESULT_INVALID_PREV", btck_BlockValidationResult_INVALID_PREV);
    ADD_INT("BLOCK_VALIDATION_RESULT_TIME_FUTURE", btck_BlockValidationResult_TIME_FUTURE);
    ADD_INT("BLOCK_VALIDATION_RESULT_HEADER_LOW_WORK",
            btck_BlockValidationResult_HEADER_LOW_WORK);

    /* Synchronization states */
    ADD_INT("SYNCHRONIZATION_STATE_INIT_REINDEX", btck_SynchronizationState_INIT_REINDEX);
    ADD_INT("SYNCHRONIZATION_STATE_INIT_DOWNLOAD", btck_SynchronizationState_INIT_DOWNLOAD);
    ADD_INT("SYNCHRONIZATION_STATE_POST_INIT", btck_SynchronizationState_POST_INIT);

    /* Warnings */
    ADD_INT("WARNING_UNKNOWN_NEW_RULES_ACTIVATED",
            btck_Warning_UNKNOWN_NEW_RULES_ACTIVATED);
    ADD_INT("WARNING_LARGE_WORK_INVALID_CHAIN", btck_Warning_LARGE_WORK_INVALID_CHAIN);

    /* Log categories */
    ADD_INT("LOG_CATEGORY_ALL", btck_LogCategory_ALL);
    ADD_INT("LOG_CATEGORY_BENCH", btck_LogCategory_BENCH);
    ADD_INT("LOG_CATEGORY_BLOCKSTORAGE", btck_LogCategory_BLOCKSTORAGE);
    ADD_INT("LOG_CATEGORY_COINDB", btck_LogCategory_COINDB);
    ADD_INT("LOG_CATEGORY_LEVELDB", btck_LogCategory_LEVELDB);
    ADD_INT("LOG_CATEGORY_MEMPOOL", btck_LogCategory_MEMPOOL);
    ADD_INT("LOG_CATEGORY_PRUNE", btck_LogCategory_PRUNE);
    ADD_INT("LOG_CATEGORY_RAND", btck_LogCategory_RAND);
    ADD_INT("LOG_CATEGORY_REINDEX", btck_LogCategory_REINDEX);
    ADD_INT("LOG_CATEGORY_VALIDATION", btck_LogCategory_VALIDATION);
    ADD_INT("LOG_CATEGORY_KERNEL", btck_LogCategory_KERNEL);

    /* Log levels */
    ADD_INT("LOG_LEVEL_TRACE", btck_LogLevel_TRACE);
    ADD_INT("LOG_LEVEL_DEBUG", btck_LogLevel_DEBUG);
    ADD_INT("LOG_LEVEL_INFO", btck_LogLevel_INFO);

#undef ADD_INT

    return module;
}
