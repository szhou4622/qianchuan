<?php
declare(strict_types=1);

if (PHP_SAPI !== 'cli') {
    exit(1);
}
$password = (string)(getenv('QCSCKP_SEED_PASSWORD') ?: '');
if ($password === '') {
    exit(2);
}
fwrite(STDOUT, password_hash($password, PASSWORD_DEFAULT));
