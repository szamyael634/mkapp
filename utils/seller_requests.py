from flask import session

def get_seller_cancellation_requests(seller_id, db):
    cursor = db.cursor(dictionary=True)
    cursor.execute('''
        SELECT o.id AS order_id, o.user_id, o.created_at, o.status, o.cancelled_reason AS reason, u.fullname AS customer_name
        FROM orders o
        JOIN users u ON o.user_id = u.id
        WHERE o.seller_id = %s AND o.status = 'cancel_request'
        ORDER BY o.created_at DESC
    ''', (seller_id,))
    requests = cursor.fetchall()
    cursor.close()
    return requests

def get_seller_refund_requests(seller_id, db):
    cursor = db.cursor(dictionary=True)
    cursor.execute('''
        SELECT DISTINCT ON (rr.id) rr.*, o.id AS order_id, o.user_id, o.created_at, o.status, u.fullname AS customer_name
        FROM order_refund_requests rr
        JOIN orders o ON rr.order_id = o.id
        JOIN order_items oi ON oi.order_id = o.id
        JOIN products p ON oi.product_id = p.id
        JOIN users u ON o.user_id = u.id
        WHERE p.seller_id = %s
        ORDER BY rr.id, rr.created_at DESC
    ''', (seller_id,))
    requests = cursor.fetchall()
    cursor.close()
    return requests
