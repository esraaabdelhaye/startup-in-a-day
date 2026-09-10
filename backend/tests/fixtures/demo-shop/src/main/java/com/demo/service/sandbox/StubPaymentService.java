package com.demo.service.sandbox;

import com.demo.domain.Order;
import com.demo.service.PaymentService;
import org.springframework.stereotype.Service;

/** Second implementation, present to exercise the multiple-implementation warning. */
@Service
public class StubPaymentService implements PaymentService {

    @Override
    public void charge(Order order) {
        order.setStatus("STUBBED");
    }
}
