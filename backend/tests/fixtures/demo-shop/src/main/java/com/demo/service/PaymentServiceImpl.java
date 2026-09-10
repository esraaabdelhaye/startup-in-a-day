package com.demo.service;

import com.demo.domain.Order;
import com.demo.repository.OrderRepository;
import org.springframework.stereotype.Service;
import org.springframework.web.client.RestTemplate;

@Service
public class PaymentServiceImpl implements PaymentService {

    private final OrderRepository orderRepository;
    private final RestTemplate restTemplate;

    public PaymentServiceImpl(OrderRepository orderRepository, RestTemplate restTemplate) {
        this.orderRepository = orderRepository;
        this.restTemplate = restTemplate;
    }

    @Override
    public void charge(Order order) {
        // Explicit this. prefix: javalang parses these as This-with-selectors,
        // not as qualified invocations.
        this.restTemplate.postForObject("https://psp.example.com/charge", order, String.class);
        order.setStatus("CHARGED");
        this.orderRepository.save(order);
    }
}
