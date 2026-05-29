INSERT INTO public.users (fullname, email, password, role, status, phone, address)
VALUES (
    'Admin User',
    'admin@mamaskitchen.com',
    'pbkdf2:sha256:600000$KXBHzZXqLJv1QmH9$3b8a6f8d1c2a5e9f7b4d1c8a3e5f2b9d1a6c4e7f8b2d5a9c1e3f6b8d1a4c7e',
    'admin',
    'active',
    '+1-800-MAMASKITCHEN',
    'Mama''s Kitchen Headquarters'
);
