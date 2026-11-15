# SI-assisted development used for code development, debugging and documentation.
# Tool: ChatGPT, Claude

"""Cola de trabajos: pausa de huérfanos, reanudación con el mismo id, carril pesado y singleflight."""
import asyncio
import dataclasses

from app import jobs as J
from app.cache import singleflight
from app.config import settings


def _mgr(monkeypatch, *, conc=2, heavy=1, queued=30, running=90):
    monkeypatch.setattr(J, "settings", dataclasses.replace(
        settings, max_concurrent_jobs=conc, max_heavy_jobs=heavy, job_orphan_queued_s=queued, job_orphan_running_s=running))
    return J.JobManager()


def _slow(gate: asyncio.Event, out: dict, runs: list):
    async def fn(job):
        runs.append(job.id)
        await gate.wait()
        return out
    return fn


def test_huerfano_en_cola_se_pausa_y_se_reanuda(monkeypatch):
    async def main():
        m = _mgr(monkeypatch, conc=1)
        gate, runs = asyncio.Event(), []
        a = m.submit("seq_aaaaaaaaaaaa", "sequence", _slow(gate, {"a": 1}, runs))
        b = m.submit("seq_bbbbbbbbbbbb", "sequence", _slow(gate, {"b": 1}, runs))
        await asyncio.sleep(0.05)
        assert a.status == "running" and b.status == "queued" and m.queue_position(b) == 1

        b.last_seen -= 31           # nadie consulta b desde hace 31 s
        m.get(a.id)                 # cualquier consulta dispara la limpieza
        await asyncio.sleep(0.05)
        assert b.paused and m.queue_position(b) == 0 and b.status == "queued"
        assert m.summary()["paused_now"] == 1

        again = m.get(b.id)         # alguien vuelve a consultarlo → se reanuda con el MISMO objeto/id
        assert again is b and not b.paused
        gate.set()
        await asyncio.sleep(0.05)
        assert a.status == "done" and b.status == "done" and b.result == {"b": 1}
        assert runs.count(b.id) == 1   # nunca había empezado: corre una sola vez
    asyncio.run(main())


def test_huerfano_corriendo_libera_el_lugar(monkeypatch):
    async def main():
        m = _mgr(monkeypatch, conc=1)
        gate, runs = asyncio.Event(), []
        a = m.submit("seq_aaaaaaaaaaab", "sequence", _slow(gate, {"a": 1}, runs))
        b = m.submit("spc_bbbbbbbbbbbc", "spectrum", _slow(gate, {"b": 1}, runs))
        await asyncio.sleep(0.05)
        assert a.status == "running" and b.status == "queued"

        a.last_seen -= 91           # el usuario se fue del objeto A
        m.get(b.id)
        await asyncio.sleep(0.05)
        assert a.paused and b.status == "running"   # B toma el lugar de A
        gate.set()
        await asyncio.sleep(0.05)
        assert b.status == "done" and a.status == "queued"  # A sigue en pausa, sin consumir nada

        m.submit(a.id, "sequence", _slow(gate, {"x": 0}, runs))   # pedirlo otra vez → se reanuda
        await asyncio.sleep(0.05)
        assert a.status == "done" and a.result == {"a": 1}
    asyncio.run(main())


def test_cubo_no_acapara_los_lugares(monkeypatch):
    async def main():
        m = _mgr(monkeypatch, conc=2, heavy=1)
        gate, runs = asyncio.Event(), []
        c1 = m.submit("cub_111111111111", "cube", _slow(gate, {}, runs))
        c2 = m.submit("cub_222222222222", "cube", _slow(gate, {}, runs))
        s = m.submit("spc_333333333333", "spectrum", _slow(gate, {}, runs))
        await asyncio.sleep(0.05)
        # Solo 1 cubo a la vez; el espectro no espera detrás del segundo cubo.
        assert c1.status == "running" and c2.status == "queued" and s.status == "running"
        gate.set()
        await asyncio.sleep(0.05)
        assert c2.status == "done"
    asyncio.run(main())


def test_duplicado_devuelve_el_mismo_trabajo(monkeypatch):
    async def main():
        m = _mgr(monkeypatch)
        gate, runs = asyncio.Event(), []
        a = m.submit("seq_dddddddddddd", "sequence", _slow(gate, {}, runs))
        b = m.submit("seq_dddddddddddd", "sequence", _slow(gate, {}, runs))
        assert a is b
        gate.set()
        await asyncio.sleep(0.05)
        assert runs == ["seq_dddddddddddd"]
    asyncio.run(main())


def test_singleflight_sobrevive_a_cancelar_al_que_descarga():
    async def main():
        calls = []

        async def download():
            calls.append(1)
            await asyncio.sleep(0.05)
            return "ok"

        leader = asyncio.create_task(singleflight("k-test", download))
        await asyncio.sleep(0.01)
        follower = asyncio.create_task(singleflight("k-test", download))
        await asyncio.sleep(0.01)
        leader.cancel()                         # se pausa el trabajo que descargaba
        assert await follower == "ok"           # el otro toma el relevo, no falla
        assert len(calls) == 2
    asyncio.run(main())
