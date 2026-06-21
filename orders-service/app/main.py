"""
orders-service
==============
Microservicio de PEDIDOS (el orquestador).

Es el corazón didáctico del laboratorio: para crear un pedido necesita
COMUNICARSE CON LOS OTROS DOS SERVICIOS vía HTTP interno:

  1. Pregunta a products-service  el precio y nombre del producto.
  2. Pide a inventory-service     que reserve (descuente) el stock.
  3. Si ambos pasos van bien, registra el pedido y calcula el total.

Las URLs de los otros servicios se inyectan por VARIABLES DE ENTORNO, para que
el MISMO código funcione tanto en docker-compose (nombre del servicio) como en
Kubernetes/EKS (DNS interno del Service ClusterIP). Esto es clave: el código no
cambia, solo cambia la configuración.

  - En docker-compose:  http://products-service:8001
  - En Kubernetes/EKS:   http://products-service:8001  (mismo nombre, DNS interno)
"""
import os

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
import time
import psutil

INICIO=time.time()
READY_MAX_MEM_PERCENT = float(os.getenv("READY_MAX_MEM_PERCENT", "90"))

app = FastAPI(
    title="Orders Service",
    description="Gestión de pedidos de la tienda (Módulo 3 - ISY1101)",
    version="1.0.0",
)

# Configuración vía variables de entorno (con valores por defecto para local).
PRODUCTS_SERVICE_URL = os.getenv("PRODUCTS_SERVICE_URL", "http://localhost:8001")
INVENTORY_SERVICE_URL = os.getenv("INVENTORY_SERVICE_URL", "http://localhost:8002")

# "Base de datos" de pedidos en memoria.
ORDERS = []


class OrderRequest(BaseModel):
    product_id: int = Field(description="Id del producto a pedir")
    quantity: int = Field(gt=0, description="Cantidad de unidades")


@app.get("/health")
def health():
    return {"status": "ok", "service": "orders-service"}

@app.get("/live")
def live():
    """Liveness: el proceso esta vivo (no depende de nadie externo)."""
    return {"alive": True, "uptime_segundos": round(time.time() - INICIO, 1)}


@app.get("/ready")
def ready():
    """Readiness basada en el uso real de CPU y memoria."""
    cpu = psutil.cpu_percent(interval=0.1)
    memoria = psutil.virtual_memory().percent
    if memoria > READY_MAX_MEM_PERCENT:
        raise HTTPException(
            status_code=503,
            detail={"ready": False, "cpu_%": cpu, "memoria_%": memoria, "umbral_%": READY_MAX_MEM_PERCENT},
        )
    return {"ready": True, "cpu_%": cpu, "memoria_%": memoria}

@app.get("/config")
def config():
    """
    Endpoint didáctico: muestra a qué URLs internas está apuntando este servicio.
    Útil para que los estudiantes comprueben la inyección por variables de entorno.
    """
    return {
        "products_service_url": PRODUCTS_SERVICE_URL,
        "inventory_service_url": INVENTORY_SERVICE_URL,
    }


@app.get("/orders")
def list_orders():
    return {"orders": ORDERS}


@app.post("/orders", status_code=201)
def create_order(order: OrderRequest):
    """
    Crea un pedido orquestando llamadas HTTP a los otros microservicios.
    """
    with httpx.Client(timeout=5.0) as client:
        # --- Paso 1: consultar el producto en products-service ---
        try:
            resp = client.get(f"{PRODUCTS_SERVICE_URL}/products/{order.product_id}")
        except httpx.RequestError as exc:
            # El servicio no responde (caído, DNS, red). 503 = dependencia no disponible.
            raise HTTPException(
                status_code=503,
                detail=f"No se pudo contactar products-service: {exc}",
            )
        if resp.status_code == 404:
            raise HTTPException(status_code=404, detail=f"Producto {order.product_id} no existe")
        resp.raise_for_status()
        product = resp.json()

        # --- Paso 2: reservar stock en inventory-service ---
        try:
            inv_resp = client.post(
                f"{INVENTORY_SERVICE_URL}/inventory/{order.product_id}/reserve",
                json={"quantity": order.quantity},
            )
        except httpx.RequestError as exc:
            raise HTTPException(
                status_code=503,
                detail=f"No se pudo contactar inventory-service: {exc}",
            )
        if inv_resp.status_code == 409:
            # Propagamos el "stock insuficiente" que originó inventory-service.
            raise HTTPException(status_code=409, detail=inv_resp.json().get("detail"))
        inv_resp.raise_for_status()
        reservation = inv_resp.json()

    # --- Paso 3: registrar el pedido ---
    order_record = {
        "order_id": len(ORDERS) + 1,
        "product_id": product["id"],
        "product_name": product["name"],
        "unit_price": product["price"],
        "quantity": order.quantity,
        "total": product["price"] * order.quantity,
        "stock_remaining": reservation["remaining"],
    }
    ORDERS.append(order_record)
    return order_record
